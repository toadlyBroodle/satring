"""End-to-end test of x402 payment flow through xpay.sh facilitator on Base Sepolia.

Steps:
1. Generate an ephemeral test wallet
2. Build an EIP-712 transferWithAuthorization signature (USDC on Base Sepolia)
3. Send to xpay.sh /verify endpoint
4. Report results

NOTE: This test only exercises /verify (signature validation), not /settle,
because settling requires the wallet to actually hold Sepolia USDC.
The /verify call confirms the facilitator is alive, accepts our payload format,
and can recover our signer address from the signature.

To test /settle end-to-end:
1. Fund the printed wallet address with Sepolia USDC from https://faucet.circle.com/
2. Run again with --settle flag
"""

import argparse
import json
import os
import secrets
import sys
import time

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

FACILITATOR_URL = "https://facilitator.xpay.sh"

# Base Sepolia testnet
NETWORK = "eip155:84532"
CHAIN_ID = 84532

# USDC on Base Sepolia (Circle's test token)
USDC_ADDRESS = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

# Recipient: burn address (doesn't matter for verify-only test)
PAY_TO = "0x000000000000000000000000000000000000dEaD"

# $0.01 = 10000 USDC base units (6 decimals)
AMOUNT = "10000"


def build_eip712_typed_data(from_addr: str) -> dict:
    """Build EIP-712 typed data for USDC transferWithAuthorization."""
    now = int(time.time())
    nonce = "0x" + secrets.token_hex(32)

    return {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name": "USDC",
            "version": "2",
            "chainId": CHAIN_ID,
            "verifyingContract": USDC_ADDRESS,
        },
        "message": {
            "from": from_addr,
            "to": PAY_TO,
            "value": int(AMOUNT),
            "validAfter": now - 600,
            "validBefore": now + 300,
            "nonce": nonce,
        },
    }


def sign_typed_data(account, typed_data: dict) -> str:
    """Sign EIP-712 typed data, return hex signature."""
    signable = encode_typed_data(full_message=typed_data)
    signed = account.sign_message(signable)
    return signed.signature.hex()


def build_verify_request(from_addr: str, signature: str, typed_data: dict) -> dict:
    """Build the /verify request body per x402 v2 spec."""
    msg = typed_data["message"]

    payment_requirements = {
        "scheme": "exact",
        "network": NETWORK,
        "asset": USDC_ADDRESS,
        "amount": AMOUNT,
        "payTo": PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {
            "name": "USDC",
            "version": "2",
        },
    }

    payment_payload = {
        "x402Version": 2,
        "resource": {
            "url": "https://satring.com/api/v1/test",
            "description": "x402 e2e test",
            "mimeType": "application/json",
        },
        "accepted": payment_requirements,
        "payload": {
            "signature": signature if signature.startswith("0x") else f"0x{signature}",
            "authorization": {
                "from": from_addr,
                "to": PAY_TO,
                "value": AMOUNT,
                "validAfter": str(msg["validAfter"]),
                "validBefore": str(msg["validBefore"]),
                "nonce": msg["nonce"],
            },
        },
    }

    return {
        "x402Version": 2,
        "paymentPayload": payment_payload,
        "paymentRequirements": payment_requirements,
    }


def main():
    parser = argparse.ArgumentParser(description="Test x402 e2e via xpay.sh facilitator")
    parser.add_argument("--settle", action="store_true", help="Also call /settle (requires funded wallet)")
    parser.add_argument("--key", help="Private key hex (without 0x prefix). If omitted, generates ephemeral wallet.")
    args = parser.parse_args()

    # Create or load wallet
    if args.key:
        account = Account.from_key(args.key)
        print(f"Using provided wallet: {account.address}")
    else:
        account = Account.create()
        print(f"Generated ephemeral wallet: {account.address}")
        print(f"Private key: {account.key.hex()}")
        print(f"(Fund with Sepolia USDC at https://faucet.circle.com/ to test --settle)")

    print()

    # Build and sign
    typed_data = build_eip712_typed_data(account.address)
    signature = sign_typed_data(account, typed_data)
    print(f"EIP-712 signature: {signature[:20]}...{signature[-8:]}")

    request_body = build_verify_request(account.address, signature, typed_data)
    print(f"\nRequest body (pretty):")
    print(json.dumps(request_body, indent=2))

    # Call /verify
    print(f"\n--- POST {FACILITATOR_URL}/verify ---")
    resp = httpx.post(f"{FACILITATOR_URL}/verify", json=request_body, timeout=30)
    print(f"Status: {resp.status_code}")
    print(f"Response: {json.dumps(resp.json(), indent=2)}")

    verify_data = resp.json()
    if verify_data.get("isValid"):
        print("\n[OK] Signature is valid! Facilitator accepted the payment.")
        recovered_payer = verify_data.get("payer", "unknown")
        print(f"Recovered payer: {recovered_payer}")

        if recovered_payer.lower() == account.address.lower():
            print("[OK] Payer matches our wallet address.")
        else:
            print("[WARN] Payer does not match wallet address!")

        if args.settle:
            print(f"\n--- POST {FACILITATOR_URL}/settle ---")
            settle_resp = httpx.post(f"{FACILITATOR_URL}/settle", json=request_body, timeout=60)
            print(f"Status: {settle_resp.status_code}")
            print(f"Response: {json.dumps(settle_resp.json(), indent=2)}")
    else:
        reason = verify_data.get("invalidReason", "unknown")
        msg = verify_data.get("invalidMessage", "")
        payer = verify_data.get("payer", "")
        print(f"\n[FAIL] Verification failed: {reason}")
        if msg:
            print(f"  Message: {msg}")
        if payer:
            print(f"  Recovered payer: {payer}")
            if payer.lower() == account.address.lower():
                print("  (Payer matches our wallet, so signing worked; issue is elsewhere)")


if __name__ == "__main__":
    main()
