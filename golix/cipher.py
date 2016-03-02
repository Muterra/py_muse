'''
LICENSING
-------------------------------------------------

golix: A python library for Golix protocol object manipulation.
    Copyright (C) 2016 Muterra, Inc.
    
    Contributors
    ------------
    Nick Badger 
        badg@muterra.io | badg@nickbadger.com | nickbadger.com

    This library is free software; you can redistribute it and/or
    modify it under the terms of the GNU Lesser General Public
    License as published by the Free Software Foundation; either
    version 2.1 of the License, or (at your option) any later version.

    This library is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
    Lesser General Public License for more details.

    You should have received a copy of the GNU Lesser General Public
    License along with this library; if not, write to the 
    Free Software Foundation, Inc.,
    51 Franklin Street, 
    Fifth Floor, 
    Boston, MA  02110-1301 USA

------------------------------------------------------

A NOTE ON RANDOM NUMBERS...
PyCryptoDome sources randomness from os.urandom(). This should be secure
for most applications. HOWEVER, if your system is low on entropy (can
be an issue in high-demand applications like servers), urandom *will not
block to wait for entropy*, and will revert (ish?) to potentially 
insufficiently secure pseudorandom generation. In that case, it might be
better to source from elsewhere (like a hardware RNG).

Some initial temporary thoughts:
1. Need to refactor signing, etc into identities.
2. Identity base class should declare supported cipher suites as a set
3. Each identity class should += the set with their support, allowing
    for easy multi-inheritance for multiple identity support
4. Identities then insert the author into the file
5. How does this interact with asymmetric objects with symmetric sigs?
    Should just look for an instance of the object? It would be nice
    to totally factor crypto awareness out of the objects entirely,
    except (of course) for address algorithms.
6. From within python, should the identies be forced to ONLY support
    a single ciphersuite? That would certainly make life easier. A 
    LOT easier. Yeah, let's do that then. Multi-CS identities can
    multi-subclass, and will need to add some kind of glue code for
    key reuse. Deal with that later, but it'll probably entail 
    backwards-incompatible changes.
7. Then, the identities should also generate secrets. That will also
    remove people from screwing up and using ex. random.random().
    But what to do with the API for that? Should identity.finalize(obj)
    return (key, obj) pair or something? That's not going to be useful
    for all objects though, because not all objects use secrets. Really,
    the question is, how to handle GEOCs in a way that makes sense?
    Maybe add an Identity.secrets(guid) attribute or summat? Though
    returning just the bytes would be really unfortunate for app
    development, because you'd have to unpack the generated bytes to
    figure out the guid. What about returning a namedtuple, and adding
    a field for secrets in the GEOC? that might be something to add
    to the actual objects (ex GEOC) instead of the identity. That would
    also reduce the burden on identities for state management of 
    generated objects, which should really be handled at a higher level
    than this library.
8. Algorithm precedence order should be defined globally, but capable
    of being overwritten

Some more temporary thoughts:
1. move all ciphersuite stuff into identities
2. put all of the generic operations like _sign into suite-dependent methods
3. Reference those operations from the first/thirdperson base class
4. Add methods like "create", "bind", "handshake", etc, to identities base
    class, creating the appropriate ex. GEOC objects and returning them,
    potentially along with a guid and (for GEOC specifically) a secret
'''

# Control * imports
__all__ = [
    'AddressAlgo1', 
    'CipherSuite1', 
    'CipherSuite2'
]

# Global dependencies
import io
import struct
import collections
import abc
import json
import base64
import os
from warnings import warn

# import Crypto
# from Crypto.Random import random
from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_OAEP as OAEP
from Crypto.Signature import pss as PSS
from Crypto.Signature.pss import MGF1
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util import Counter
from Crypto.Protocol.KDF import HKDF
from Crypto.Hash import HMAC
from donna25519 import PrivateKey as ECDHPrivate
from donna25519 import PublicKey as ECDHPublic

from smartyparse import ParseError

# Interpackage dependencies
from .utils import Guid
from .utils import SecurityError
from .utils import ADDRESS_ALGOS
from .utils import Secret

from .utils import AsymHandshake
from .utils import AsymAck
from .utils import AsymNak

from .utils import _dummy_asym
from .utils import _dummy_mac
from .utils import _dummy_signature
from .utils import _dummy_address
from .utils import _dummy_guid
from .utils import _dummy_pubkey

from ._getlow import GIDC
from ._getlow import GEOC
from ._getlow import GOBS
from ._getlow import GOBD
from ._getlow import GDXX
from ._getlow import GARQ

from ._getlow import GARQHandshake
from ._getlow import GARQAck
from ._getlow import GARQNak

# Some globals
DEFAULT_ADDRESSER = 1
DEFAULT_CIPHER = 1


# Some utilities
class _FrozenHash():
    ''' Somewhat-janky utility PyCryptoDome-specific base class for 
    creating fake hash objects from already-generated hash digests. 
    Looks like a hash, acts like a hash (where appropriate), but doesn't
    carry a state, and all mutability functions are disabled.
    
    On a scale from 1-to-complete-hack, this is probably 2-3 Baja.
    '''
        
    def __init__(self, data):
        if len(data) != self.digest_size:
            raise ValueError('Passed frozen data does not match digest size of hash.')
            
        self._data = data
        
    def update(self, data):
        raise TypeError('Frozen hashes cannot be updated.')
        
    def copy(self):
        raise TypeError('Frozen hashes cannot be copied.')
        
    def digest(self):
        return self._data
    

class _FrozenSHA512(_FrozenHash, SHA512.SHA512Hash):
    pass
    
    
class _IdentityBase(metaclass=abc.ABCMeta):
    def __init__(self, keys, author_guid):
        self._author_guid = author_guid
        
        try:
            self._signature_key = keys['signature']
            self._encryption_key = keys['encryption']
            self._exchange_key = keys['exchange']
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                'Generating ID from existing keys requires dict-like obj '
                'with "signature", "encryption", and "exchange" keys.'
            ) from e
    
    @property
    def author_guid(self):
        return self._author_guid
        
    @property
    def ciphersuite(self):
        return self._ciphersuite
        
    @classmethod
    def _dispatch_address(cls, address_algo):
        if address_algo == 'default':
            address_algo = cls.DEFAULT_ADDRESS_ALGO
        elif address_algo not in ADDRESS_ALGOS:
            raise ValueError(
                'Address algorithm unavailable for use: ' + str(address_algo)
            )
        return address_algo
        
    @classmethod
    def _typecheck_secret(cls, secret):
        # Awkward but gets the job done
        if not isinstance(secret, Secret):
            return False
        if secret.cipher != cls._ciphersuite:
            return False
        return True
    
    
class _ThirdPersonBase(metaclass=abc.ABCMeta):
    @classmethod
    def from_keys(cls, keys, address_algo):
        try:
            # Turn them into bytes first.
            packed_keys = cls._pack_keys(keys)
        except (KeyError, TypeError) as e:
            raise RuntimeError(
                'Generating ID from existing keys requires dict-like obj '
                'with "signature", "encryption", and "exchange" keys.'
            ) from e
            
        gidc = GIDC( 
            signature_key=packed_keys['signature'],
            encryption_key=packed_keys['encryption'],
            exchange_key=packed_keys['exchange']
        )
        gidc.pack(cipher=cls._ciphersuite, address_algo=address_algo)
        author_guid = gidc.guid
        self = cls(keys=keys, author_guid=author_guid)
        self.packed = gidc.packed
        return self
        
    @classmethod
    @abc.abstractmethod
    def _pack_keys(cls, keys):
        ''' Convert self.keys from objects used for crypto operations
        into bytes-like objects suitable for output into a GIDC.
        '''
        pass
        
        
class _FirstPersonBase(metaclass=abc.ABCMeta):
    DEFAULT_ADDRESS_ALGO = DEFAULT_ADDRESSER
    
    def __init__(self, keys=None, author_guid=None, address_algo='default', *args, **kwargs):
        self.address_algo = self._dispatch_address(address_algo)
        
        # Load an existing identity
        if keys is not None and author_guid is not None:
            pass
            
        # Catch any improper declaration
        elif keys is not None or author_guid is not None:
            raise TypeError(
                'Generating an ID manually from existing keys requires '
                'both keys and author_guid.'
            )
            
        # Generate a new identity
        else:
            keys = self._generate_keys()
            self._third_party = self._generate_third_person(keys, self.address_algo)
            author_guid = self._third_party.author_guid
            
        # Now dispatch super() with the adjusted keys, author_guid
        super().__init__(keys=keys, author_guid=author_guid, *args, **kwargs)
        
    @classmethod
    def _typecheck_thirdparty(cls, obj):
        # Type check the partner. Must be ThirdPersonIdentityX or similar.
        if not isinstance(obj, cls._3PID):
            raise TypeError(
                'Object must be a ThirdPersonIdentity of compatible type '
                'with the FirstPersonIdentity initiating the request/ack/nak.'
            )
        else:
            return True
    
    @property
    def third_party(self):
        return self._third_party
         
    def make_object(self, secret, plaintext):
        if not self._typecheck_secret(secret):
            raise TypeError(
                'Secret must be a properly-formatted Secret compatible with '
                'the current identity\'s declared ciphersuite.'
            )
        
        geoc = GEOC(author=self.author_guid)
        geoc.payload = self._encrypt(secret, plaintext)
        geoc.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(geoc.guid.address)
        geoc.pack_signature(signature)
        # This will need to be converted into a namedtuple or something
        return geoc.guid, geoc.packed
        
    def make_bind_static(self, guid):        
        gobs = GOBS(
            binder = self.author_guid,
            target = guid
        )
        gobs.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gobs.guid.address)
        gobs.pack_signature(signature)
        return gobs.guid, gobs.packed
        
    def make_bind_dynamic(self, guid, address=None, history=None):
        gobd = GOBD(
            binder = self.author_guid,
            target = guid,
            dynamic_address = address,
            history = history
        )
        gobd.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gobd.guid.address)
        gobd.pack_signature(signature)
        return gobd.guid, gobd.packed, gobd.dynamic_address
        
    def make_debind(self, guid):
        gdxx = GDXX(
            debinder = self.author_guid,
            target = guid
        )
        gdxx.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        signature = self._sign(gdxx.guid.address)
        gdxx.pack_signature(signature)
        return gdxx.guid, gdxx.packed
        
    def make_request(self, secret, target, recipient):
        self._typecheck_thirdparty(recipient)
        
        request = GARQHandshake(
            author = self.author_guid,
            target = target,
            secret = secret
        )
        request.pack()
        garq = self._make_asym(
            recipient = recipient,
            plaintext = request.packed
        )
        return garq.guid, garq.packed
        
    def make_ack(self, target, recipient, status=0):
        self._typecheck_thirdparty(recipient)
        
        ack = GARQAck(
            author = self.author_guid,
            target = target,
            status = status
        )
        ack.pack()
        garq = self._make_asym(
            recipient = recipient,
            plaintext = ack.packed
        )
        return garq.guid, garq.packed
        
    def make_nak(self, target, recipient, status=0):
        self._typecheck_thirdparty(recipient)
        
        nak = GARQNak(
            author = self.author_guid,
            target = target,
            status = status
        )
        nak.pack()
        garq = self._make_asym(
            recipient = recipient,
            plaintext = nak.packed
        )
        return garq.guid, garq.packed
        
    def _make_asym(self, recipient, plaintext):
        # Convert the plaintext to a proper payload and create a garq from it
        payload = self._encrypt_asym(recipient, plaintext)
        del plaintext
        garq = GARQ(
            recipient = recipient.author_guid,
            payload = payload
        )
        
        # Pack 'er up and generate a MAC for it
        garq.pack(cipher=self.ciphersuite, address_algo=self.address_algo)
        garq.pack_signature(
            self._mac(
                key = self._derive_shared(recipient),
                data = garq.guid.address
            )
        )
        return garq
        
    def unpack_request(self, packed):
        garq = GARQ.unpack(packed)
        plaintext = self._decrypt_asym(garq.payload)
        
        # Try all object handlers available for asymmetric payloads
        parse_success = False
        # Could do this with a loop, but it gets awkward when trying to
        # assign stuff to the resulting object.
        try:
            unpacked = GARQHandshake.unpack(plaintext)
            request = AsymHandshake(
                author = unpacked.author,
                target = unpacked.target, 
                secret = unpacked.secret
            )
        except ParseError:
            try:
                unpacked = GARQAck.unpack(plaintext)
                request = AsymAck(
                    author = unpacked.author,
                    target = unpacked.target, 
                    status = unpacked.status
                )
            except ParseError:
                try:
                    unpacked = GARQNak.unpack(plaintext)
                    request = AsymNak(
                        author = unpacked.author,
                        target = unpacked.target, 
                        status = unpacked.status
                    )
                except ParseError:
                    raise SecurityError('Could not securely unpack request.')
            
        garq._plaintext = request
        
        return request.author, garq
        
    def receive_request(self, public, request):
        ''' Verifies the request and exposes its contents.
        '''
        # Typecheck all the things
        self._typecheck_thirdparty(public)
        # Also make sure the request is something we've already unpacked
        if not isinstance(request, GARQ):
            raise TypeError(
                'Request must be an unpacked GARQ, as returned from '
                'unpack_request.'
            )
        try:
            plaintext = request._plaintext
        except AttributeError as e:
            raise TypeError(
                'Request must be an unpacked GARQ, as returned from '
                'unpack_request.'
            ) from e
            
        self._verify_mac(
            key = self._derive_shared(public),
            data = request.guid.address,
            mac = request.signature
        )
        
        del request._plaintext
        return plaintext
        
    def unpack_object(self, packed):
        geoc = GEOC.unpack(packed)
        return geoc.author, geoc
    
    def receive_object(self, public, secret, obj):
        if not isinstance(obj, GEOC):
            raise TypeError(
                'Obj must be an unpacked GEOC, for example, as returned from '
                'unpack_object.'
            )
        
        signature = obj.signature
        self._verify(public, signature, obj.guid.address)
        plaintext = self._decrypt(secret, obj.payload)
        # This will need to be converted into a namedtuple or something
        return obj.guid, plaintext
        
    @classmethod
    @abc.abstractmethod
    def _generate_third_person(cls, keys, address_algo):
        ''' MUST ONLY be called when generating one from scratch, not 
        when loading one. Loading must always be done directly through
        loading a ThirdParty.
        '''
        pass
        
    @abc.abstractmethod
    def _generate_keys(self):
        pass
    
    @classmethod
    @abc.abstractmethod
    def new_secret(cls, *args, **kwargs):
        ''' Placeholder method to create new symmetric secret. Returns
        a Secret().
        '''
        return Secret(cipher=cls._ciphersuite, *args, **kwargs)
        
    @abc.abstractmethod
    def _sign(self, data):
        ''' Placeholder signing method.
        '''
        pass
        
    @abc.abstractmethod
    def _verify(self, public, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        '''
        pass
        
    @abc.abstractmethod
    def _encrypt_asym(self, public, data):
        ''' Placeholder asymmetric encryptor.
        '''
        pass
        
    @abc.abstractmethod
    def _decrypt_asym(self, data):
        ''' Placeholder asymmetric decryptor.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _decrypt(cls, secret, data):
        ''' Placeholder symmetric decryptor.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _encrypt(cls, secret, data):
        ''' Placeholder symmetric encryptor.
        '''
        pass
        
    @abc.abstractmethod
    def _derive_shared(self, partner):
        ''' Derive a shared secret (not necessarily a Secret!) with the 
        partner.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _mac(cls, key, data):
        ''' Generate a MAC for data using key.
        '''
        pass
        
    @classmethod
    @abc.abstractmethod
    def _verify_mac(cls, key, mac, data):
        ''' Generate a MAC for data using key.
        '''
        pass
    
        
class ThirdPersonIdentity0(_ThirdPersonBase, _IdentityBase):
    _ciphersuite = 0
        
    @classmethod
    def _pack_keys(cls, keys):
        return keys
        
        
class FirstPersonIdentity0(_FirstPersonBase, _IdentityBase):
    ''' FOR TESTING PURPOSES ONLY. 
    
    Entirely inoperative. Correct API, but ignores all input, creating
    only a symbolic output.
    
    NOTE THAT INHERITANCE ORDER MATTERS! Must be first a FirstPerson, 
    and second an Identity.
    '''
    _ciphersuite = 0
    _3PID = ThirdPersonIdentity0
        
    # Well it's not exactly repeating yourself, though it does mean there
    # are sorta two ways to perform decryption. Best practice = always decrypt
    # using the author's ThirdPersonIdentity
        
    @classmethod
    def _generate_third_person(cls, keys, address_algo):
        keys = {}
        keys['signature'] = _dummy_pubkey
        keys['encryption'] = _dummy_pubkey
        keys['exchange'] = _dummy_pubkey
        return cls._3PID.from_keys(keys, address_algo)
        
    def _generate_keys(self):
        keys = {}
        keys['signature'] = _dummy_pubkey
        keys['encryption'] = _dummy_pubkey
        keys['exchange'] = _dummy_pubkey
        return keys
    
    @classmethod
    def new_secret(cls):
        ''' Placeholder method to create new symmetric secret.
        '''
        return super().new_secret(key=bytes(32), seed=None)
        
    def _sign(self, data):
        ''' Placeholder signing method.
        
        Data must be bytes-like. Private key should be a dictionary 
        formatted with all necessary components for a private key (?).
        '''
        return _dummy_signature
    
    def _verify(self, public, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        self._typecheck_thirdparty(public)
        return True
        
    def _encrypt_asym(self, public, data):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        self._typecheck_thirdparty(public)
        return _dummy_asym
        
    def _decrypt_asym(self, data):
        ''' Placeholder asymmetric decryptor.
        
        Maybe add kwarguments do define what kind of internal object is
        returned? That would be smart.
        
        Or, even better, do an arbitrary object content, and then encode
        what class of internal object to use there. That way, it's not
        possible to accidentally encode secrets publicly, but you can 
        also emulate behavior of normal exchange.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        # Note that this will error out when trying to load components,
        # since it's 100% an invalid declaration of internal content.
        # But, it's a good starting point.
        return _dummy_asym
        
    @classmethod
    def _decrypt(cls, secret, data):
        ''' Placeholder symmetric decryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER DECRYPTED SYMMETRIC MESSAGE. Hello world! ]]'
        
    @classmethod
    def _encrypt(cls, secret, data):
        ''' Placeholder symmetric encryptor.
        
        Data should be bytes-like. Key should be bytes-like.
        '''
        return b'[[ PLACEHOLDER ENCRYPTED SYMMETRIC MESSAGE. Hello, world? ]]'
    
    def _derive_shared(self, partner):
        ''' Derive a shared secret with the partner.
        '''
        self._typecheck_thirdparty(partner)
        return b'[[ Placeholder shared secret ]]'
        
    @classmethod
    def _mac(cls, key, data):
        ''' Generate a MAC for data using key.
        '''
        return _dummy_mac
        
    @classmethod
    def _verify_mac(cls, key, mac, data):
        return True
        

class ThirdPersonIdentity1(_ThirdPersonBase, _IdentityBase): 
    _ciphersuite = 1  
        
    @classmethod
    def _pack_keys(cls, keys):
        packkeys = {
            'signature': int.to_bytes(keys['signature'].n, length=512, byteorder='big'),
            'encryption': int.to_bytes(keys['encryption'].n, length=512, byteorder='big'),
            'exchange': keys['exchange'].public,
        }
        return packkeys


# Signature constants.
# Put these here because 1. explicit and 2. what if PCD API changes?
# Explicit is better than implicit!
# Don't include these in the class 1. to avoid cluttering it and 2. to avoid
# accidentally passing self
_PSS_SALT_LENGTH = SHA512.digest_size
_PSS_MGF = lambda x, y: MGF1(x, y, SHA512)
# example calls:
# h = _FrozenSHA512(data)
# PSS.new(private_key, mask_func=PSS_MGF, salt_bytes=PSS_SALT_LENGTH).sign(h)
# or, on the receiving end:
# PSS.new(public_key, mask_func=PSS_MGF, salt_bytes=PSS_SALT_LENGTH).verify(h, signature)
# Verification returns nothing (=None) if successful, raises ValueError if not
class FirstPersonIdentity1(_FirstPersonBase, _IdentityBase):
    ''' ... Hmmm
    '''
    _ciphersuite = 1
    _3PID = ThirdPersonIdentity1
        
    # Well it's not exactly repeating yourself, though it does mean there
    # are sorta two ways to perform decryption. Best practice = always decrypt
    # using the author's ThirdPersonIdentity
        
    @classmethod
    def _generate_third_person(cls, keys, address_algo):
        pubkeys = {
            'signature': keys['signature'].publickey(),
            'encryption': keys['encryption'].publickey(),
            'exchange': keys['exchange'].get_public()
        } 
        del keys
        return cls._3PID.from_keys(keys=pubkeys, address_algo=address_algo)
        
    @classmethod
    def _generate_keys(cls):
        keys = {}
        keys['signature'] = RSA.generate(4096)
        keys['encryption'] = RSA.generate(4096)
        keys['exchange'] = ECDHPrivate()
        return keys
    
    @classmethod
    def new_secret(cls):
        ''' Returns a new secure Secret().
        '''
        key = get_random_bytes(32)
        nonce = get_random_bytes(16)
        return super().new_secret(key=key, seed=nonce)
        
    @classmethod
    def _encrypt(cls, secret, data):
        ''' Symmetric encryptor.
        '''
        # Courtesy of pycryptodome's API limitations:
        if not isinstance(data, bytes):
            data = bytes(data)
        # Convert the secret's seed (nonce) into an integer for pycryptodome
        ctr_start = int.from_bytes(secret.seed, byteorder='big')
        ctr = Counter.new(nbits=128, initial_value=ctr_start)
        cipher = AES.new(key=secret.key, mode=AES.MODE_CTR, counter=ctr)
        return cipher.encrypt(data)
        
    @classmethod
    def _decrypt(cls, secret, data):
        ''' Symmetric decryptor.
        
        Handle multiple ciphersuites by having a thirdpartyidentity for
        whichever author created it, and calling their decrypt instead.
        '''
        # Courtesy of pycryptodome's API limitations:
        if not isinstance(data, bytes):
            data = bytes(data)
        # Convert the secret's seed (nonce) into an integer for pycryptodome
        ctr_start = int.from_bytes(secret.seed, byteorder='big')
        ctr = Counter.new(nbits=128, initial_value=ctr_start)
        cipher = AES.new(key=secret.key, mode=AES.MODE_CTR, counter=ctr)
        return cipher.decrypt(data)
        
    def _sign(self, data):
        ''' Signing method.
        '''
        h = _FrozenSHA512(data)
        signer = PSS.new(
            self._signature_key, 
            mask_func=_PSS_MGF, 
            salt_bytes=_PSS_SALT_LENGTH
        )
        return signer.sign(h)
       
    def _verify(self, public, signature, data):
        ''' Verifies an author's signature against bites. Errors out if 
        unsuccessful. Returns True if successful.
        
        Data must be bytes-like. public_key should be a dictionary 
        formatted with all necessary components for a public key (?).
        Signature must be bytes-like.
        '''
        self._typecheck_thirdparty(public)
        
        h = _FrozenSHA512(data)
        signer = PSS.new(public._signature_key, mask_func=_PSS_MGF, salt_bytes=_PSS_SALT_LENGTH)
        try:
            signer.verify(h, signature)
        except ValueError as e:
            raise SecurityError('Failed to verify signature.') from e
            
        return True
        
    def _encrypt_asym(self, public, data):
        ''' Placeholder asymmetric encryptor.
        
        Data should be bytes-like. Public key should be a dictionary 
        formatted with all necessary components for a public key.
        '''
        self._typecheck_thirdparty(public)
        cipher = OAEP.new(public._encryption_key)
        return cipher.encrypt(data)
        
    def _decrypt_asym(self, data):
        ''' Placeholder asymmetric decryptor.
        '''
        cipher = OAEP.new(self._encryption_key)
        plaintext = cipher.decrypt(data)
        del cipher
        return plaintext
    
    def _derive_shared(self, partner):
        ''' Derive a shared secret with the partner.
        '''
        # Call the donna25519 exchange method and return bytes
        ecdh = self._exchange_key.do_exchange(partner._exchange_key)
        
        # Get both of our addresses and then the bitwise XOR of them both
        my_hash = self.author_guid.address
        their_hash = partner.author_guid.address
        salt = bytes([a ^ b for a, b in zip(my_hash, their_hash)])
        
        key = HKDF(
            master = ecdh, 
            key_len = SHA512.digest_size,
            salt = salt,
            hashmod = SHA512
        )
        # Might as well do this immediately, not that it really adds anything
        del ecdh, my_hash, their_hash, salt
        return key
        
    @classmethod
    def _mac(cls, key, data):
        ''' Generate a MAC for data using key.
        '''
        h = HMAC.new(
            key = key,
            msg = data,
            digestmod = SHA512
        )
        d = h.digest()
        # Do this "just in case" to prevent accidental future updates
        del h
        return d
        
    @classmethod
    def _verify_mac(cls, key, mac, data):
        ''' Verify an existing MAC.
        '''
        mac = bytes(mac)
        data = bytes(data)
        
        h = HMAC.new(
            key = key,
            msg = data,
            digestmod = SHA512
        )
        try:
            h.verify(mac)
        except ValueError as e:
            raise SecurityError('Failed to verify MAC.') from e
            
        return True
        