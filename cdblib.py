'''
Manipulate DJB's Constant Databases. These are 2 level disk-based hash tables
that efficiently handle many keys, while remaining space-efficient.

    http://cr.yp.to/cdb.html

'''

from _struct import Struct
from itertools import chain


def py_djb_hash(s):
    '''Return the value of DJB's hash function for the given 8-bit string.'''
    h = 5381
    for c in s:
        h = (((h << 5) + h) ^ ord(c)) & 0xffffffff
    return h

try:
    from _cdblib import djb_hash
except ImportError:
    djb_hash = py_djb_hash

read_2_le4 = Struct('<LL').unpack
read_2_le8 = Struct('<QQ').unpack
write_2_le4 = Struct('<LL').pack
write_2_le8 = Struct('<QQ').pack


class Reader(object):
    '''A dictionary-like object for reading a Constant Database accessed
    through a string or string-like sequence, such as mmap.mmap().'''

    read_pair = staticmethod(read_2_le4)
    pair_size = 8

    def __init__(self, data, hashfn=djb_hash):
        '''Create an instance reading from a sequence and using hashfn to hash
        keys.'''
        if len(data) < 2048:
            raise IOError('CDB too small')

        self.data = data
        self.hashfn = hashfn
        self.index = [self.read_pair(data[i:i+self.pair_size])
                      for i in xrange(0, 256*self.pair_size, self.pair_size)]
        self.table_start = min(p[0] for p in self.index)
        # Assume load load factor is 0.5 like official CDB.
        self.length = sum(p[1] >> 1 for p in self.index)

    def iteritems(self):
        '''Like dict.iteritems(). Items are returned in insertion order.'''
        pos = self.pair_size * 256
        while pos < self.table_start:
            klen, dlen = self.read_pair(self.data[pos:pos+self.pair_size])
            pos += self.pair_size

            key = self.data[pos:pos+klen]
            pos += klen

            data = self.data[pos:pos+dlen]
            pos += dlen

            yield key, data

    def items(self):
        '''Like dict.items().'''
        return list(self.iteritems())

    def iterkeys(self):
        '''Like dict.iterkeys().'''
        return (p[0] for p in self.iteritems())
    __iter__ = iterkeys

    def itervalues(self):
        '''Like dict.itervalues().'''
        return (p[1] for p in self.iteritems())

    def keys(self):
        '''Like dict.keys().'''
        return [p[0] for p in self.iteritems()]

    def values(self):
        '''Like dict.values().'''
        return [p[1] for p in self.iteritems()]

    def __getitem__(self, key):
        '''Like dict.__getitem__().'''
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def has_key(self, key):
        '''Return True if key exists in the database.'''
        return self.get(key) is not None
    __contains__ = has_key

    def __len__(self):
        '''Return the number of records in the database.'''
        return self.length

    def gets(self, key):
        '''Yield values for key in insertion order.'''
        # Truncate to 32 bits and remove sign.
        h = self.hashfn(key) & 0xffffffff
        start, nslots = self.index[h & 0xff]

        if nslots:
            end = start + (nslots * self.pair_size)
            slot_off = start + (((h >> 8) % nslots) * self.pair_size)

            for pos in chain(xrange(slot_off, end, self.pair_size),
                             xrange(start, slot_off, self.pair_size)):
                rec_h, rec_pos = self.read_pair(
                    self.data[pos:pos+self.pair_size]
                )

                if not rec_h:
                    break
                elif rec_h == h:
                    klen, dlen = self.read_pair(
                        self.data[rec_pos:rec_pos+self.pair_size]
                    )
                    rec_pos += self.pair_size

                    if self.data[rec_pos:rec_pos+klen] == key:
                        rec_pos += klen
                        yield self.data[rec_pos:rec_pos+dlen]

    def get(self, key, default=None):
        '''Get the first value for key, returning default if missing.'''
        # Avoid exception catch when handling default case; much faster.
        return chain(self.gets(key), (default,)).next()

    def getint(self, key, default=None, base=0):
        '''Get the first value for key converted it to an int, returning
        default if missing.'''
        value = self.get(key, default)
        if value is not default:
            return int(value, base)
        return value

    def getints(self, key, base=0):
        '''Yield values for key in insertion order after converting to int.'''
        return (int(v, base) for v in self.gets(key))

    def getstring(self, key, default=None, encoding='utf-8'):
        '''Get the first value for key decoded as unicode, returning default if
        not found.'''
        value = self.get(key, default)
        if value is not default:
            return value.decode(encoding)
        return value

    def getstrings(self, key, encoding='utf-8'):
        '''Yield values for key in insertion order after decoding as
        unicode.'''
        return (v.decode(encoding) for v in self.gets(key))


class Reader64(Reader):
    '''A cdblib.Reader variant to support reading from CDB files that use
    64-bit file offsets. The CDB file must be generated with an appropriate
    writer.'''

    read_pair = staticmethod(read_2_le8)
    pair_size = 16


class Writer(object):
    '''Object for building new Constant Databases, and writing them to a
    seekable file-like object.'''

    write_pair = staticmethod(write_2_le4)
    pair_size = 8

    def __init__(self, fp, hashfn=djb_hash):
        '''Create an instance writing to a file-like object, using hashfn to
        hash keys.'''
        self.fp = fp
        self.hashfn = hashfn

        fp.write('\x00' * (256 * self.pair_size))
        self._unordered = [[] for i in xrange(256)]

    def put(self, key, value=''):
        '''Write a string key/value pair to the output file.'''
        assert type(key) is str and type(value) is str

        pos = self.fp.tell()
        self.fp.write(self.write_pair(len(key), len(value)))
        self.fp.write(key)
        self.fp.write(value)

        h = self.hashfn(key) & 0xffffffff
        self._unordered[h & 0xff].append((h, pos))

    def puts(self, key, values):
        '''Write more than one value for the same key to the output file.
        Equivalent to calling put() in a loop.'''
        for value in values:
            self.put(key, value)

    def putint(self, key, value):
        '''Write an integer as a base-10 string associated with the given key
        to the output file.'''
        self.put(key, str(value))

    def putints(self, key, values):
        '''Write zero or more integers for the same key to the output file.
        Equivalent to calling putint() in a loop.'''
        self.puts(key, (str(value) for value in values))

    def putstring(self, key, value, encoding='utf-8'):
        '''Write a unicode string associated with the given key to the output
        file after encoding it as UTF-8 or the given encoding.'''
        self.put(key, unicode.encode(value, encoding))

    def putstrings(self, key, values, encoding='utf-8'):
        '''Write zero or more unicode strings to the output file. Equivalent to
        calling putstring() in a loop.'''
        self.puts(key, (unicode.encode(value, encoding) for value in values))

    def finalize(self):
        '''Write the final hash tables to the output file, and write out its
        index. The output file remains open upon return.'''
        index = []
        for tbl in self._unordered:
            length = len(tbl) * 2
            ordered = [(0, 0)] * length
            for pair in tbl:
                where = (pair[0] >> 8) % length
                for i in chain(xrange(where, length), xrange(0, where)):
                    if not ordered[i][0]:
                        ordered[i] = pair
                        break

            index.append((self.fp.tell(), length))
            for pair in ordered:
                self.fp.write(self.write_pair(*pair))

        self.fp.seek(0)
        for pair in index:
            self.fp.write(self.write_pair(*pair))
        self.fp = None # prevent double finalize()


class Writer64(Writer):
    '''A cdblib.Writer variant to support writing CDB files that use 64-bit
    file offsets.'''

    write_pair = staticmethod(write_2_le8)
    pair_size = 16
