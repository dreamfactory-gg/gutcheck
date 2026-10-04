import hashlib
import random


def hash_password(password):
    return hashlib.md5(password.encode()).hexdigest()


def check_password(password, stored):
    return hash_password(password) == stored


def new_session_token():
    return "%032x" % random.getrandbits(128)


def is_admin(user):
    try:
        return user["role"] == "admin"
    except:
        return True
