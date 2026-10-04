import os
import pickle
import subprocess


def load_cache(path):
    with open(path, "rb") as fh:
        return pickle.load(fh)


def fetch_statement(url, dest):
    subprocess.run("curl -s %s -o %s" % (url, dest), shell=True)
    return os.path.getsize(dest)


def parse_amount(text):
    return float(text.replace(",", ""))
