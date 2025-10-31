import json
import asyncio
import subprocess
import base64
import base58
import httpx
import requests
import time
from flask import Flask, request, jsonify
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction, Transaction
from solders.message import to_bytes_versioned
from solders.signature import Signature
from solana.rpc.async_api import AsyncClient
from solana.rpc.api import Client
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Confirmed
from spl.token.instructions import get_associated_token_address
from solders.pubkey import Pubkey
import aiohttp
import logging
from typing import Optional, Dict, Any
import threading

def run_async_task(coro):
    def runner():
        asyncio.run(coro)
    threading.Thread(target=runner).start()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- Configuration ---
WALLET_FILE = "wallet.json"
PASSPHRASE = ""  # Change this to your secret passphrase

# Token addresses
FARTCOIN_MINT = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Raydium API configuration
RAYDIUM_QUOTE_URL = "https://transaction-v1.raydium.io/compute/swap-base-in"
RAYDIUM_TX_URL = "https://transaction-v1.raydium.io/transaction/swap-base-in"
RPC_URL = "https://mainnet.helius-rpc.com/?api-key="

# Telegram configuration
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

class RaydiumSwap:
    def __init__(self, wallet: Keypair, rpc_url: str = RPC_URL):
        """Initialize Raydium swap client"""
        self.wallet = wallet
        self.rpc_url = rpc_url
        self.wallet_address = str(wallet.pubkey())
        self.wallet_pubkey = wallet.pubkey()

        # Get Associated Token Accounts (ATAs)
        self.fartcoin_ata = str(get_associated_token_address(
            self.wallet_pubkey,
            Pubkey.from_string(FARTCOIN_MINT)
        ))
        self.usdc_ata = str(get_associated_token_address(
            self.wallet_pubkey,
            Pubkey.from_string(USDC_MINT)
        ))

    async def get_quote(self,
                       input_mint: str,
                       output_mint: str,
                       amount: int,
                       slippage_bps: int = 100,
                       tx_version: str = "V0") -> Dict[Any, Any]:
        """Get swap quote from Raydium"""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount,
            "slippageBps": slippage_bps,
            "txVersion": tx_version,
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(RAYDIUM_QUOTE_URL, params=params, timeout=10) as resp:
                resp.raise_for_status()
                data = await resp.json()

        logger.info(f"Fetched quote ID: {data.get('id')}")
        if 'data' in data:
            quote_data = data['data']
            input_amount = int(quote_data.get('inputAmount', 0))
            output_amount = int(quote_data.get('outputAmount', 0))

            # Display human-readable amounts (6 decimals for both FARTCOIN and USDC)
            input_readable = input_amount / 1e6
            output_readable = output_amount / 1e6

            if input_mint == FARTCOIN_MINT:
                logger.info(f"Quote: {input_readable:.6f} FARTCOIN → {output_readable:.6f} USDC")
            else:
                logger.info(f"Quote: {input_readable:.6f} USDC → {output_readable:.6f} FARTCOIN")

        return data

    async def execute_swap(self,
                          swap_response: dict,
                          input_mint: str,
                          output_mint: str,
                          compute_unit_price: str = "10000",
                          tx_version: str = "V0") -> dict:
        """Execute the swap transaction"""
        # Determine correct token accounts based on mints
        if input_mint == FARTCOIN_MINT:
            input_account = self.fartcoin_ata
            output_account = self.usdc_ata
        else:
            input_account = self.usdc_ata
            output_account = self.fartcoin_ata

        payload = {
            "swapResponse": swap_response,
            "wallet": self.wallet_address,
            "inputAccount": input_account,
            "outputAccount": output_account,
            "wrapSol": False,
            "unwrapSol": False,
            "computeUnitPriceMicroLamports": compute_unit_price,
            "txVersion": tx_version,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(RAYDIUM_TX_URL, json=payload, timeout=10) as resp:
                resp.raise_for_status()
                tx_data = await resp.json()

        # Extract transaction data
        tx_b64 = None

        if "swapTransaction" in tx_data:
            tx_b64 = tx_data["swapTransaction"]
        elif "transaction" in tx_data:
            tx_b64 = tx_data["transaction"]
        elif "data" in tx_data:
            data_section = tx_data["data"]
            if isinstance(data_section, list) and len(data_section) > 0:
                if "transaction" in data_section[0]:
                    tx_b64 = data_section[0]["transaction"]
                elif "swapTransaction" in data_section[0]:
                    tx_b64 = data_section[0]["swapTransaction"]
            elif isinstance(data_section, dict):
                if "transaction" in data_section:
                    tx_b64 = data_section["transaction"]
                elif "swapTransaction" in data_section:
                    tx_b64 = data_section["swapTransaction"]

        if not tx_b64:
            raise ValueError(f"Could not find transaction data in API response")

        # Sign and send transaction
        raw = base64.b64decode(tx_b64)

        if tx_version == "V0":
            vt = VersionedTransaction.from_bytes(raw)
            vt = VersionedTransaction(vt.message, [self.wallet])
            send_bytes = bytes(vt)
        else:
            tx = Transaction.deserialize(raw)
            tx.sign(self.wallet)
            send_bytes = tx.serialize()

        async with AsyncClient(self.rpc_url) as client:
            res = await client.send_raw_transaction(send_bytes)
            sig = getattr(res, "value", None) or res.get("result")

            if isinstance(sig, str):
                sig_obj = Signature.from_string(sig)
            else:
                sig_obj = sig

            tx_hash = ""
            if sig_obj is not None:
                # Wait a bit for transaction to likely be confirmed
                await asyncio.sleep(5)
                # Do a single confirmation check instead of polling
                try:
                    status = await client.get_signature_statuses([sig_obj])
                    if status.value[0] is not None:
                        tx_hash = str(sig_obj)
                except:
                    # If check fails, still return the signature
                    tx_hash = str(sig_obj)

        result = {"tx_hash": tx_hash, "response": tx_data}
        logger.info(f"✅ Swap executed - Transaction Hash: {tx_hash}")
        logger.info(f"View on Solscan: https://solscan.io/tx/{tx_hash}")
        return result


    async def swap(self,
                   input_mint: str,
                   output_mint: str,
                   amount: int,
                   slippage_bps: int = 100,
                   max_retries: int = 2) -> Dict[Any, Any]:
        """Complete swap process: get quote and execute"""
        for attempt in range(max_retries):
            try:
                logger.info(f"Raydium swap attempt {attempt + 1}/{max_retries}")

                # Step 1: Get quote
                logger.info("Getting swap quote...")
                quote = await self.get_quote(input_mint, output_mint, amount, slippage_bps)

                # Step 2: Execute swap
                logger.info("Executing swap...")
                result = await self.execute_swap(
                    swap_response=quote,
                    input_mint=input_mint,
                    output_mint=output_mint
                )

                tx_hash = result.get("tx_hash")
                if tx_hash:
                    logger.info(f"✅ Swap successful!")
                    logger.info(f"Transaction signature: {tx_hash}")
                    return {"status": "Success", "signature": tx_hash, "tx_hash": tx_hash}
                else:
                    logger.error(f"❌ Swap failed: No transaction hash returned")
                    if attempt < max_retries - 1:
                        logger.info("Retrying...")
                        await asyncio.sleep(2)
                    else:
                        return {"status": "Failed", "error": "No transaction hash returned"}

            except Exception as e:
                logger.error(f"❌ Error on attempt {attempt + 1}: {str(e)}")
                if attempt < max_retries - 1:
                    logger.info("Retrying...")
                    await asyncio.sleep(2)
                else:
                    raise e

def get_wallet() -> Keypair:
    """Loads the wallet keypair from the wallet.json file."""
    try:
        with open(WALLET_FILE, 'r') as f:
            wallet_data = json.load(f)
            secret_key_b58 = wallet_data["secretKey"]
            if isinstance(secret_key_b58, str):
                return Keypair.from_base58_string(secret_key_b58)
            else:
                return Keypair.from_bytes(bytes(secret_key_b58))
    except Exception as e:
        logger.error(f"Error loading wallet: {e}")
        raise

async def send_telegram_message(message: str):
    """Send a message to Telegram group."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as session:
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }
            response = await session.post(TELEGRAM_API_URL, json=payload)
            if response.status_code == 200:
                logger.info("Telegram message sent successfully")
            else:
                logger.error(f"Failed to send Telegram message: {response.text}")
    except Exception as e:
        logger.error(f"Error sending Telegram message: {e}")

def get_token_balance(wallet_address: str, token_mint: str) -> float:
    """Get token balance using spl-token command."""
    try:
        cmd = ["spl-token", "accounts", "--owner", wallet_address, "--output", "json"]
        logger.info(f"Executing command: {' '.join(cmd)}")

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"Command output: {result.stdout}")

        if result.stderr:
            logger.warning(f"Command stderr: {result.stderr}")

        accounts = json.loads(result.stdout)

        for account in accounts.get("accounts", []):
            if account["mint"] == token_mint:
                balance = account["tokenAmount"]["uiAmount"]
                if balance is None:
                    balance = 0.0
                logger.info(f"Found balance for {token_mint}: {balance}")
                return float(balance)

        logger.info(f"No account found for mint: {token_mint}")
        return 0.0

    except subprocess.CalledProcessError as e:
        logger.error(f"spl-token command failed: {e}")
        logger.error(f"Return code: {e.returncode}")
        logger.error(f"Stdout: {e.stdout}")
        logger.error(f"Stderr: {e.stderr}")
        return 0.0
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON output: {e}")
        logger.error(f"Raw output: {result.stdout}")
        return 0.0
    except Exception as e:
        logger.error(f"Error getting token balance: {e}")
        return 0.0

async def execute_swap_raydium(input_mint: str, output_mint: str, amount: float, token_symbol: str):
    """Execute swap using Raydium API."""
    try:
        wallet = get_wallet()

        # Convert amount to proper decimals
        if input_mint == USDC_MINT or input_mint == FARTCOIN_MINT:
            amount_in_base_units = int(amount * 1e6)  # 6 decimals for USDC and FARTCOIN
        else:
            amount_in_base_units = int(amount * 1e9)  # 9 decimals for SOL

        # Initialize Raydium swap client
        raydium = RaydiumSwap(wallet)

        # Execute swap
        result = await raydium.swap(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount_in_base_units,
            slippage_bps=100  # 1% slippage
        )

        if result.get('status') == 'Success':
            tx_signature = result.get('signature')
            success_message = (
                f"✅ <b>Swap Successful!</b>\n\n"
                f"🔄 <b>Action:</b> Swapped {amount:.2f} {token_symbol}\n"
                f"📝 <b>Signature:</b> <code>{tx_signature}</code>\n"
                f"🔗 <a href='https://solscan.io/tx/{tx_signature}'>View on Solscan</a>"
            )
            await send_telegram_message(success_message)
            return True, f"Transaction confirmed: {tx_signature}"
        else:
            error_message = f"❌ Swap failed: {result.get('error', 'Unknown error')}"
            await send_telegram_message(error_message)
            return False, f"Swap failed: {result.get('error', 'Unknown error')}"

    except Exception as e:
        error_message = f"❌ <b>Swap Error:</b> {str(e)}"
        await send_telegram_message(error_message)
        logger.error(f"Error in execute_swap_raydium: {e}")
        return False, str(e)

@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle TradingView webhook."""
    try:
        data = request.get_json()

        # Validate request
        if not data:
            return jsonify({"error": "No JSON data provided"}), 400

        if data.get("passphrase") != PASSPHRASE:
            return jsonify({"error": "Invalid passphrase"}), 401

        message = data.get("message", "").lower()
        if message not in ["buy", "sell"]:
            return jsonify({"error": "Invalid message. Use 'buy' or 'sell'"}), 400

        # Process the signal asynchronously
        run_async_task(process_signal(message))

        return jsonify({"status": "success", "message": f"Signal '{message}' received"}), 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"error": str(e)}), 500

async def process_signal(signal: str):
    """Process buy/sell signal using Raydium API."""
    try:
        wallet = get_wallet()
        wallet_address = str(wallet.pubkey())

        logger.info(f"Processing {signal} signal for wallet: {wallet_address}")

        if signal == "buy":
            # Check USDC balance
            usdc_balance = get_token_balance(wallet_address, USDC_MINT)
            logger.info(f"USDC Balance: {usdc_balance}")

            if usdc_balance < 1.0:
                error_message = f"❌ <b>Buy Signal Failed:</b> Insufficient USDC balance ({usdc_balance:.2f})"
                await send_telegram_message(error_message)
                return

            # Use available USDC balance (truncated to 2 decimals)
            amount_to_swap = int(usdc_balance * 100) / 100

            alert_message = f"🔄 <b>Buy Signal Received!</b>\n\n💰 Swapping {amount_to_swap:.2f} USDC → FARTCOIN"
            await send_telegram_message(alert_message)

            success, message = await execute_swap_raydium(USDC_MINT, FARTCOIN_MINT, amount_to_swap, "USDC")

        else:  # sell
            # Check FARTCOIN balance
            fartcoin_balance = get_token_balance(wallet_address, FARTCOIN_MINT)
            logger.info(f"FARTCOIN Balance: {fartcoin_balance}")

            if fartcoin_balance < 1.0:
                error_message = f"❌ <b>Sell Signal Failed:</b> Insufficient FARTCOIN balance ({fartcoin_balance:.2f})"
                await send_telegram_message(error_message)
                return

            # Use available FARTCOIN balance (truncated to 2 decimals)
            amount_to_swap = int(fartcoin_balance * 100) / 100

            alert_message = f"🔄 <b>Sell Signal Received!</b>\n\n💰 Swapping {amount_to_swap:.2f} FARTCOIN → USDC"
            await send_telegram_message(alert_message)

            success, message = await execute_swap_raydium(FARTCOIN_MINT, USDC_MINT, amount_to_swap, "FARTCOIN")

        if not success:
            logger.error(f"Swap failed: {message}")

    except Exception as e:
        error_message = f"❌ <b>Signal Processing Error:</b> {str(e)}"
        await send_telegram_message(error_message)
        logger.error(f"Error processing signal: {e}")

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "message": "Bot is running with Raydium"}), 200


@app.route('/balance', methods=['GET'])
def check_balance():
    """Check wallet balances."""
    try:
        wallet = get_wallet()
        wallet_address = str(wallet.pubkey())

        usdc_balance = get_token_balance(wallet_address, USDC_MINT)
        fartcoin_balance = get_token_balance(wallet_address, FARTCOIN_MINT)

        return jsonify({
            "wallet": wallet_address,
            "balances": {
                "USDC": usdc_balance,
                "FARTCOIN": fartcoin_balance
            }
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("🚀 Starting SOL Trading Bot with Raydium...")
    print(f"📝 Webhook URL: http://your-server/webhook")
    print(f"🔍 Health Check: http://your-server/health")
    print(f"💰 Balance Check: http://your-server/balance")

    # Test Telegram connection on startup
    run_async_task(send_telegram_message("🤖 <b>Trading Bot Started with Raydium!</b>\n\nReady to process TradingView signals."))

    app.run(host='0.0.0.0', port=80, debug=False)
