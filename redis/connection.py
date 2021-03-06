import errno
import socket
import Queue
from itertools import chain, imap
from redis.exceptions import ConnectionError, ResponseError, InvalidResponse

class PythonParser(object):
    def __init__(self):
        self._fp = None

    def on_connect(self, connection):
        "Called when the socket connects"
        self._fp = connection._sock.makefile('r')

    def on_disconnect(self):
        "Called when the socket disconnects"
        if self._fp is not None:
            self._fp.close()
            self._fp = None

    def read(self, length=None):
        """
        Read a line from the socket is no length is specified,
        otherwise read ``length`` bytes. Always strip away the newlines.
        """
        try:
            if length is not None:
                return self._fp.read(length+2)[:-2]
            return self._fp.readline()[:-2]
        except (socket.error, socket.timeout), e:
            raise ConnectionError("Error while reading from socket: %s" % \
                (e.args,))

    def read_response(self):
        response = self.read()
        if not response:
            raise ConnectionError("Socket closed on remote end")

        byte, response = response[0], response[1:]

        # server returned an error
        if byte == '-':
            if response.startswith('ERR '):
                response = response[4:]
                return ResponseError(response)
            if response.startswith('LOADING '):
                # If we're loading the dataset into memory, kill the socket
                # so we re-initialize (and re-SELECT) next time.
                raise ConnectionError("Redis is loading data into memory")
        # single value
        elif byte == '+':
            return response
        # int value
        elif byte == ':':
            return long(response)
        # bulk response
        elif byte == '$':
            length = int(response)
            if length == -1:
                return None
            response = self.read(length)
            return response
        # multi-bulk response
        elif byte == '*':
            length = int(response)
            if length == -1:
                return None
            return [self.read_response() for i in xrange(length)]
        raise InvalidResponse("Protocol Error")

class HiredisParser(object):
    def on_connect(self, connection):
        self._sock = connection._sock
        self._reader = hiredis.Reader(
            protocolError=InvalidResponse,
            replyError=ResponseError)

    def on_disconnect(self):
        self._sock = None
        self._reader = None

    def read_response(self):
        if not self._reader:
            raise ConnectionError("Socket closed on remote end")
        response = self._reader.gets()
        while response is False:
            try:
                buffer = self._sock.recv(4096)
            except (socket.error, socket.timeout), e:
                raise ConnectionError("Error while reading from socket: %s" % \
                    (e.args,))
            if not buffer:
                raise ConnectionError("Socket closed on remote end")
            self._reader.feed(buffer)
            # proactively, but not conclusively, check if more data is in the
            # buffer. if the data received doesn't end with \n, there's more.
            if not buffer.endswith('\n'):
                continue
            response = self._reader.gets()
        return response

try:
    import hiredis
    DefaultParser = HiredisParser
except ImportError:
    DefaultParser = PythonParser

class Connection(object):
    "Manages TCP communication to and from a Redis server"
    def __init__(self, host='localhost', port=6379, db=0, password=None,
                 socket_timeout=None, encoding='utf-8',
                 encoding_errors='strict', parser_class=DefaultParser):
        self.host = host
        self.port = port
        self.db = db
        self.password = password
        self.socket_timeout = socket_timeout
        self.encoding = encoding
        self.encoding_errors = encoding_errors
        self._sock = None
        self._parser = parser_class()

    def connect(self):
        "Connects to the Redis server if not already connected"
        if self._sock:
            return
        try:
            sock = self._connect()
        except socket.error, e:
            raise ConnectionError(self._error_message(e))

        self._sock = sock
        self.on_connect()

    def _connect(self):
        "Create a TCP socket connection"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.socket_timeout)
        sock.connect((self.host, self.port))
        return sock

    def _error_message(self, exception):
        # args for socket.error can either be (errno, "message")
        # or just "message"
        if len(exception.args) == 1:
            return "Error connecting to %s:%s. %s." % \
                (self.host, self.port, exception.args[0])
        else:
            return "Error %s connecting %s:%s. %s." % \
                (exception.args[0], self.host, self.port, exception.args[1])


    def on_connect(self):
        "Initialize the connection, authenticate and select a database"
        self._parser.on_connect(self)

        # if a password is specified, authenticate
        if self.password:
            self.send_command('AUTH', self.password)
            if self.read_response() != 'OK':
                raise ConnectionError('Invalid Password')

        # if a database is specified, switch to it
        if self.db:
            self.send_command('SELECT', self.db)
            if self.read_response() != 'OK':
                raise ConnectionError('Invalid Database')

    def disconnect(self):
        "Disconnects from the Redis server"
        self._parser.on_disconnect()
        if self._sock is None:
            return
        try:
            self._sock.close()
        except socket.error:
            pass
        self._sock = None

    def send_packed_command(self, command):
        "Send an already packed command to the Redis server"
        if not self._sock:
            self.connect()
        try:
            self._sock.sendall(command)
        except socket.error, e:
            self.disconnect()
            if len(e.args) == 1:
                _errno, errmsg = 'UNKNOWN', e.args[0]
            else:
                _errno, errmsg = e.args
            raise ConnectionError("Error %s while writing to socket. %s." % \
                (_errno, errmsg))
        except:
            self.disconnect()
            raise

    def send_command(self, *args):
        "Pack and send a command to the Redis server"
        self.send_packed_command(self.pack_command(*args))

    def read_response(self):
        "Read the response from a previously sent command"
        try:
            response = self._parser.read_response()
        except:
            self.disconnect()
            raise
        if response.__class__ == ResponseError:
            raise response
        return response

    def encode(self, value):
        "Return a bytestring representation of the value"
        if isinstance(value, unicode):
            return value.encode(self.encoding, self.encoding_errors)
        return str(value)

    def pack_command(self, *args):
        "Pack a series of arguments into a value Redis command"
        command = ['$%s\r\n%s\r\n' % (len(enc_value), enc_value)
                   for enc_value in imap(self.encode, args)]
        return '*%s\r\n%s' % (len(command), ''.join(command))

class UnixDomainSocketConnection(Connection):
    def __init__(self, path='', db=0, password=None,
                 socket_timeout=None, encoding='utf-8',
                 encoding_errors='strict', parser_class=DefaultParser):
        self.path = path
        self.db = db
        self.password = password
        self.socket_timeout = socket_timeout
        self.encoding = encoding
        self.encoding_errors = encoding_errors
        self._sock = None
        self._parser = parser_class()

    def _connect(self):
        "Create a Unix domain socket connection"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.socket_timeout)
        sock.connect(self.path)
        return sock

    def _error_message(self, exception):
        # args for socket.error can either be (errno, "message")
        # or just "message"
        if len(exception.args) == 1:
            return "Error connecting to unix socket: %s. %s." % \
                (self.path, exception.args[0])
        else:
            return "Error %s connecting to unix socket: %s. %s." % \
                (exception.args[0], self.path, exception.args[1])


class ConnectionPool(object):
    "Generic connection pool"
    def __init__(self, connection_class=Connection, max_connections=None,
                 **connection_kwargs):
        self.connection_class = connection_class
        self.connection_kwargs = connection_kwargs
        self.max_connections = max_connections or 100 # anything more is useless
        self._connections = []
        self._available_connections = Queue.LifoQueue()
        self._in_use_connections = set()
        for _ in xrange(self.max_connections):
            connection = self.connection_class(**self.connection_kwargs)
            self._connections.append(connection)
            self._available_connections.put(connection)

    def get_connection(self, command_name, *keys, **options):
        "Get a connection from the pool"
        try:
            connection = self._available_connections.get(block=options.get("block", True))
            self._in_use_connections.add(connection)
            return connection
        except Queue.Empty:
            raise ConnectionError("No connection available.")

    def release(self, connection):
        "Releases the connection back to the pool"
        self._in_use_connections.remove(connection)
        self._available_connections.put(connection)

    def disconnect(self):
        "Disconnects all connections in the pool"
        for connection in self._connections:
            connection.disconnect()
