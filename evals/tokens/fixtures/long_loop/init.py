"""One-shot release init: prints the release facts once, then removes itself."""
import os

print("release codename: VERMILION-HARE-31")
print("build number: 48213")
print("artifact checksum: c0ffee9a1d")
os.remove(__file__)
