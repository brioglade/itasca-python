## see: https://docs.python.org/2/howto/sockets.html
import numpy as np
import struct
import socket
import select
import time


class _fileSocketAdapter(object):
    """This object is an adapter which allows np.save and np.load to write directly to the socket. This object appears to be a file object but does reading and writing over a socket connection. """
    def __init__(self, s):
        "(s: _baseSocket) -> None. Constructor."
        self.s=s
        self.first = True
        self.offset = 0

    def write(self, data):
        "(data: str) -> None. Write bytes to stream."
        self.s._sendall(data)

    def read(self, byte_count):
        "(byte_count: int) -> str. Read bytes from stream."
        bytes_read = 0
        data = ''
        # this is a hack because we have to support seek for np.load
        if self.offset:
            assert self.offset <= byte_count
            assert self.offset==6
            assert not self.first
            data += self.buff
            bytes_read += self.offset
            self.offset = 0
        while bytes_read < byte_count:
            self.s.wait_for_data()
            data_in = self.s.conn.recv(min(4096, byte_count-bytes_read))
            data += data_in
            bytes_read += len(data_in)
        # this is a hack because we have to support seek for np.load
        if self.first and byte_count==6:
            self.buff = data
            self.first = False
        return data

    def readline(self):
        "() -> str. Read a line from the stream."
        data=''
        while True:
            self.s.wait_for_data()
            byte = self.s.conn.recv(1)
            if byte == '\n':
                return data
            else:
                data += byte
        return data

    def seek(self, a0, a1):
        "(offset: int, mode: int) -> None. This is a hack to support np.load and np.save talking over sockets."
        assert a1 == 1
        assert a0 == -6
        assert len(self.buff)==6
        self.offset = 6



class _socketBase(object):
    code = 12345

    def _sendall(self, data):
        """(bytes: str) -> None. Low level socket send, do not call this function directly."""
        nbytes = len(data)
        sent = 0
        while sent < nbytes:
            self._wait_for_write()
            sent += self.conn.send(data[sent:])


    def _wait_for_write(self):
        """() -> None. Block until socket is write ready but let thread scheduler run. """
        while True:
            _, write_ready, _ = select.select([], [self.conn], [], 0.0)
            if write_ready: break
            else: time.sleep(1e-8)


    def send_data(self, value):
        """(value: any) -> None. Send value. value must be a number, a string or a NumPy array. """
        if type(value) == int:
            self._sendall(struct.pack("i", 1))
            self._sendall(struct.pack("i", value))
        elif type(value) == float:
            self._sendall(struct.pack("i", 2))
            self._sendall(struct.pack("d", value))
        elif type(value) == list and len(value)==2:
            float_list = [float(x) for x in value]
            self._sendall(struct.pack("i", 5))
            self._sendall(struct.pack("dd", float_list[0], float_list[1]))
        elif type(value) == list and len(value)==3:
            float_list = [float(x) for x in value]
            self._sendall(struct.pack("i", 6))
            self._sendall(struct.pack("ddd", float_list[0],
                                          float_list[1], float_list[2]))
        elif type(value) == str:
            length = len(value)
            self._sendall(struct.pack("ii", 3, length))
            buffer_length = 4*(1+(length-1)/4)
            format_string = "%is" % buffer_length
            value += " "*(buffer_length - length)
            self._sendall(struct.pack(format_string, value))
        elif type(value) == np.ndarray:
            self._sendall(struct.pack("i", 7))
            np.save(_fileSocketAdapter(self), value)
        else:
            raise Exception("unknown type in send_data")

    def wait_for_data(self):
        """() -> None. Block until data is available. This call allows the Python thread scheduler to run.
        """
        while True:
            input_ready, _, _ = select.select([self.conn],[],[], 0.0)
            if input_ready: return
            else: time.sleep(1e-8)

    def read_type(self, type_string, array_bytes=None):
        """(type: str) -> any. This method should not be called directly. Use the read_data method.
        """
        if array_bytes is None:
            byte_count = struct.calcsize(type_string)
        else:
            byte_count = array_bytes
        bytes_read = 0
        data = ''
        while bytes_read < byte_count:
            self.wait_for_data()
            data_in = self.conn.recv(min(4096,byte_count - bytes_read))
            data += data_in
            bytes_read += len(data_in)
        assert len(data)==byte_count, "bad packet data"
        return data

    def read_data(self):
        """() -> any. Read the next item from the socket connection."""
        raw_data = self.read_type("i")
        type_code, = struct.unpack("i", raw_data)
        if type_code == 1:     # int
            raw_data = self.read_type("i")
            value, = struct.unpack("i", raw_data)
            return value
        elif type_code == 2:   # float
            raw_data = self.read_type("d")
            value, = struct.unpack("d", raw_data)
            return value
        elif type_code == 3:   # string
            length_data = self.read_type("i")
            length, = struct.unpack("i", length_data)
            buffer_length = (4*(1+(length-1)/4))
            format_string = "%is" % buffer_length
            data = self.read_type(format_string)
            return data [:length]
        elif type_code == 5:   # V2
            raw_data = self.read_type("dd")
            value0, value1 = struct.unpack("dd", raw_data)
            return [value0, value1]
        elif type_code == 6:   # V3
            raw_data = self.read_type("ddd")
            value0, value1, value3 = struct.unpack("ddd", raw_data)
            return [value0, value1, value3]
        elif type_code == 7:  # NumPy array:
            a = np.load(_fileSocketAdapter(self))
            return a
        assert False, "Data read type error"

    def close(self):
        """() -> None. Close the active socket connection."""
        if hasattr(self, "conn"):
            self.conn.shutdown(socket.SHUT_RDWR)
            self.conn.close()
        else:
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()

    def __enter__(self):
        return self

    def __exit__(self, eType, eValue, eTrace):
        print "cleaning up socket"
        self.close()

class p2pLinkServer(_socketBase):
    """Python to Python socket link server. Send and receive numbers, strings
    and NumPy arrays between Python instances."""
    def __init__(self, port=5000):
        """(port=5000) -> None. Create a Python to Python socket server. Call
        the start() method to open the connection."""
        assert type(port) is int
        self.port = port

    def start(self):
        """() -> None. Open the socket connection. Blocks but allows the
        Python thread scheduler to run.
        """
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("", self.port))
        self.socket.listen(1)
        while True:
            connected, _, _ = select.select([self.socket], [], [], 0.0)
            if connected: break
            else: time.sleep(1e-8)
        self.conn, addr = self.socket.accept()
        assert self.read_data() == _socketBase.code
        print "got code"

class p2pLinkClient(_socketBase):
    """Python to Python socket link client. Send and receive numbers, strings
    and NumPy arrays between Python instances."""
    def __init__(self,port=5000):
        """(port=5000) -> None. Create a Python to Python socket link client.
        Call the start() method to open the connection.""
        """
        assert type(port) is int
        self.port = port
    def connect(self, machine):
        """(machine: str) -> None. Connect to a Python to Python link server.
        """
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((machine,self.port))
        self.conn = self.socket
        self.send_data(_socketBase.code)
        print "sent code"
