const crypto = require('node:crypto');

// GitHub Secrets may contain accidental surrounding whitespace or a copied 0x prefix.
// Normalize only these harmless formatting differences; never print credential values.
function normalizeHex(value) {
  return String(value || '').trim().replace(/^0x/i, '');
}

const apiKey = normalizeHex(process.env.COINSWITCH_API_KEY);
const secretHex = normalizeHex(process.env.COINSWITCH_SECRET_KEY);
const baseUrl = 'https://coinswitch.co';

function fail(message) {
  console.error('CoinSwitch API check: ' + message);
  process.exit(1);
}

if (!/^[0-9a-fA-F]{64}$/.test(apiKey)) fail('API key missing or not a 32-byte hex public key.');
if (!/^[0-9a-fA-F]{64}$/.test(secretHex)) fail('Secret key missing or not a 32-byte hex private seed.');

async function main() {
  const epoch = String(Date.now());
  const path = '/trade/api/v2/validate/keys';
  const message = 'GET' + path + epoch;
  const seed = Buffer.from(secretHex, 'hex');
  const der = Buffer.concat([
    Buffer.from('302e020100300506032b657004220420', 'hex'),
    seed,
  ]);
  const privateKey = crypto.createPrivateKey({ key: der, format: 'der', type: 'pkcs8' });
  const signature = crypto.sign(null, Buffer.from(message, 'utf8'), privateKey).toString('hex');

  const response = await fetch(baseUrl + path, {
    method: 'GET',
    headers: {
      'Content-Type': 'application/json',
      'X-AUTH-APIKEY': apiKey,
      'X-AUTH-SIGNATURE': signature,
      'X-AUTH-EPOCH': epoch,
    },
    signal: AbortSignal.timeout(15000),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const safeMessage = typeof body.message === 'string' ? body.message : ('HTTP ' + response.status);
    fail('authentication rejected (' + response.status + '): ' + safeMessage);
  }
  if (body.message !== 'Valid Access') {
    fail('unexpected validation response; check CoinSwitch API response in Actions log.');
  }
  console.log('CoinSwitch API check: VALID ACCESS. Credentials work. No orders were placed.');
}

main().catch((error) => fail(error.message || 'request failed'));
