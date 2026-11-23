import os, time, urllib.parse, urllib.request, json
from cryptography.hazmat.primitives.asymmetric import ed25519

BASE = "https://coinswitch.co"

def signed_request(method, path):
    api_key = os.getenv("COINSWITCH_API_KEY", "")
    secret_hex = os.getenv("COINSWITCH_SECRET_KEY", "")
    if not api_key or not secret_hex:
        raise RuntimeError("CoinSwitch API credentials are not configured in Render environment")
    secret = bytes.fromhex(secret_hex)
    if len(secret) != 32:
        raise RuntimeError("CoinSwitch secret key format is invalid")
    epoch = str(int(time.time() * 1000))
    message = method.upper() + urllib.parse.unquote_plus(path) + epoch
    signature = ed25519.Ed25519PrivateKey.from_private_bytes(secret).sign(message.encode()).hex()
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": api_key,
        "X-AUTH-SIGNATURE": signature,
        "X-AUTH-EPOCH": epoch,
    }
    req = urllib.request.Request(BASE + path, headers=headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.status, json.loads(response.read().decode())

def validate_keys():
    return signed_request("GET", "/trade/api/v2/validate/keys")
