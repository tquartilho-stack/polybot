from dotenv import load_dotenv
load_dotenv()
import os
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2

creds = ApiCreds(
    api_key        = os.getenv("POLY_API_KEY"),
    api_secret     = os.getenv("POLY_API_SECRET"),
    api_passphrase = os.getenv("POLY_API_PASSPHRASE"),
)

client = ClobClient(
    host           = "https://clob.polymarket.com",
    chain_id       = 137,
    key            = os.getenv("POLY_PRIVATE_KEY"),
    creds          = creds,
    signature_type = 3,
    funder         = "0x0F4902690951B760C451A8f9dc81D72871359E18",
)

print("OK:", client.get_ok())
print("Funder:", client.builder.funder)

market = client.get_market("0xf305528c5dbf4f080f8d96ec4fb2047c89aec3923bcce237eb4238e9dd588a09")
tokens = market.get("tokens", [])
token_id = tokens[0]["token_id"] if tokens else None
print("Token:", token_id[:20] if token_id else None)

if token_id:
    try:
        order = client.create_and_post_order(OrderArgsV2(
            token_id = token_id,
            price    = 0.50,
            size     = 5.0,
            side     = "BUY",
        ))
        print("SUCESSO:", order)
    except Exception as e:
        print("ERRO:", e)