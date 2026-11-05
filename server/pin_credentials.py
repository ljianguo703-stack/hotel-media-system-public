"""Offline PIN verifier. Never publish the fast six-digit PIN digest."""
import hashlib
import re


def public_verifier(pin, device_id):
    if not isinstance(pin, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(pin.get('revision',''))) or not re.fullmatch(r'[a-f0-9]{64}', str(pin.get('sha256',''))):
        return None
    salt = hashlib.sha256((pin['revision'] + ':' + device_id).encode()).digest()[:16]
    rounds = 210000
    verifier = hashlib.pbkdf2_hmac('sha1',pin['sha256'].encode('ascii'),salt,rounds,32).hex()
    return {'revision':pin['revision'],'algorithm':'pbkdf2-sha1-of-sha256','salt':salt.hex(),'iterations':rounds,'verifier':verifier}
