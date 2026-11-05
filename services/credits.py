"""
Report-unlock credits ledger service.

Wallet changes use the Supabase RPCs in migrations/004_credits.sql and
migrations/009_scan_unlock.sql. The unlock RPC charges and marks the
stored report unlocked in one transaction.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.db_service import DatabasePersistenceService

logger = logging.getLogger("resync.credits")

# Credits are real: a new account starts with whatever its wallet holds (nothing, after migration 015) and every
# unlock spends one. The only way to switch that off is an explicit testing flag; the old CREDITS_ENABLED=false setting
# that made every account show 999 credits is no longer read, so it cannot be left on by mistake.
CREDITS_ENABLED = os.getenv("CREDITS_UNLIMITED_FOR_TESTING", "false").strip().lower() not in ("true", "1", "yes")
UNLIMITED_BALANCE = 999


class InsufficientCreditsError(Exception):
    """Raised when a user has no scan credits left. Caught in main.py and
    translated to HTTP 402 Payment Required."""

    def __init__(self, user_id: str, balance: int = 0) -> None:
        self.user_id = user_id
        self.balance = balance
        super().__init__(f"User {user_id} has insufficient credits (balance={balance}).")


class CreditsService:

    @staticmethod
    def _client():
        return DatabasePersistenceService.get_client()

    @classmethod
    async def get_balance(cls, user_id: str) -> int:
        if not CREDITS_ENABLED:
            return UNLIMITED_BALANCE
        import asyncio
        client = cls._client()

        def _fetch():
            resp = (
                client.table("credit_wallet")
                .select("balance")
                .eq("user_id", user_id)
                .execute()
            )
            return resp.data[0]["balance"] if resp.data else 0

        return await asyncio.to_thread(_fetch)

    @classmethod
    async def unlock_report(cls, user_id: str, analysis_run_id: str) -> Dict[str, Any]:
        """Run the database's atomic report unlock transaction."""
        import asyncio
        client = cls._client()

        def _call():
            return client.rpc("unlock_scan_report", {
                "p_user_id": user_id,
                "p_analysis_run_id": analysis_run_id,
                "p_charge": CREDITS_ENABLED,
            }).execute().data

        return await asyncio.to_thread(_call)

    @classmethod
    async def debit_scan_credit(cls, user_id: str, analysis_run_id: str) -> int:
        """Atomically debits one credit for a scan. Raises
        InsufficientCreditsError if the user's balance is 0."""
        if not CREDITS_ENABLED:
            return UNLIMITED_BALANCE
        import asyncio
        client = cls._client()
        idempotency_key = f"scan_debit:{analysis_run_id}"

        def _call():
            return client.rpc("debit_scan_credit", {
                "p_user_id": user_id,
                "p_analysis_run_id": analysis_run_id,
                "p_idempotency_key": idempotency_key,
            }).execute()

        try:
            resp = await asyncio.to_thread(_call)
            return resp.data
        except Exception as exc:
            if "INSUFFICIENT_CREDITS" in str(exc):
                balance = await cls.get_balance(user_id)
                raise InsufficientCreditsError(user_id, balance) from exc
            raise

    @classmethod
    async def refund_scan_credit(cls, user_id: str, analysis_run_id: str) -> Optional[int]:
        """Refunds the credit for a scan that failed after being debited.
        A failed scan must never cost the student a credit. Never raises --
        a refund failure is logged, not surfaced, since the scan failure
        itself is already the thing the caller needs to report."""
        if not CREDITS_ENABLED:
            return None
        import asyncio
        client = cls._client()
        idempotency_key = f"scan_debit:{analysis_run_id}"

        def _call():
            return client.rpc("refund_scan_credit", {
                "p_user_id": user_id,
                "p_analysis_run_id": analysis_run_id,
                "p_idempotency_key": f"refund:{idempotency_key}",
            }).execute()

        try:
            resp = await asyncio.to_thread(_call)
            return resp.data
        except Exception as exc:
            logger.error("Failed to refund credit for user %s, run %s: %s", user_id, analysis_run_id, exc)
            return None

    @classmethod
    async def create_checkout(cls, user_id: str, credit_amount: int, unit_price: float = 25.0) -> Dict[str, Any]:
        """Inserts a pending pymt_txn row and returns a mock checkout
        reference. This is the seam a real PayMongo/Stripe checkout-session
        call would occupy -- everything downstream (confirm -> RPC ->
        ledger) stays the same regardless of provider."""
        import asyncio
        client = cls._client()
        txn_id = str(uuid.uuid4())
        total_amount = round(credit_amount * unit_price, 2)

        def _insert():
            client.table("pymt_txn").insert({
                "pymt_txn_id": txn_id,
                "user_id": user_id,
                "pymt_txn_amount": total_amount,
                "pymt_txn_status": "pending",
                "pymt_txn_credits": credit_amount,
                "pymt_txn_provider": "simulated",
            }).execute()

        await asyncio.to_thread(_insert)
        return {
            "pymt_txn_id": txn_id,
            "checkout_reference": f"SIM-{txn_id[:8].upper()}",
            "amount": total_amount,
            "credit_amount": credit_amount,
        }

    # What the app sells. The server decides the price, so a changed request cannot buy credits for less.
    PACKAGES = {1: 25.0, 5: 25.0, 20: 22.5, 50: 20.0}   # credits -> price per credit (PHP)
    METHODS = ("card", "gcash", "bank_transfer")

    @classmethod
    async def purchase(cls, user_id: str, credit_amount: int, method: str = "card",
                       proof: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Records the purchase and adds the credits to the wallet in one step.

        No money moves yet: this is where a payment provider's confirmation would go. Everything else is real: the
        transaction is saved, the wallet balance rises, and the ledger shows a purchase entry the person can see.
        """
        import asyncio
        if credit_amount not in cls.PACKAGES:
            raise ValueError("Choose one of the credit packages.")
        if method not in cls.METHODS:
            raise ValueError("Unknown payment method.")
        client = cls._client()
        txn_id = str(uuid.uuid4())
        total_amount = round(credit_amount * cls.PACKAGES[credit_amount], 2)
        row = {"pymt_txn_id": txn_id, "pymt_txn_amount": total_amount, "pymt_txn_status": "pending",
               "pymt_txn_credits": credit_amount, "pymt_txn_provider": method,
               "pymt_txn_created_at": datetime.now(timezone.utc).isoformat()}

        def _record():
            # Payments in this project belong to the buyer's subscription record.
            try:
                found = (client.table("subscription").select("subscription_id")
                         .eq("user_id", user_id).order("subscription_started_at", desc=True).limit(1).execute())
                if found.data:
                    row["subscription_id"] = found.data[0]["subscription_id"]
            except Exception as error:
                logger.warning("Could not look up the subscription for user %s: %s", user_id, error)
            try:
                client.table("pymt_txn").insert({**row, "user_id": user_id}).execute()
            except Exception as error:
                # Projects set up before migration 014 have no user_id column here; the ledger still names the buyer.
                if "user_id" not in str(error):
                    raise
                client.table("pymt_txn").insert(row).execute()

        await asyncio.to_thread(_record)

        def _mark(status: str):
            try:
                client.table("pymt_txn").update({"pymt_txn_status": status}).eq("pymt_txn_id", txn_id).execute()
            except Exception as error:
                logger.error("Could not mark transaction %s as %s: %s", txn_id, status, error)

        if proof:
            # The receipt is a keepsake while no payment provider confirms the money: if it cannot be stored (for
            # example migration 012 has not been run), the purchase still goes through.
            try:
                await asyncio.to_thread(lambda: client.table("payment_proof").insert(
                    {"proof_id": str(uuid.uuid4()), "pymt_txn_id": txn_id, "user_id": user_id, "method": method, **proof}).execute())
            except Exception as error:
                logger.warning("Proof of payment for %s was not stored: %s", txn_id, error)
        try:
            balance = await asyncio.to_thread(lambda: client.rpc("purchase_credits", {
                "p_user_id": user_id, "p_amount": credit_amount, "p_pymt_txn_id": txn_id,
                "p_idempotency_key": f"purchase:{txn_id}"}).execute().data)
        except Exception:
            await asyncio.to_thread(_mark, "failed")   # a purchase that did not finish adds nothing
            raise
        await asyncio.to_thread(_mark, "paid")
        return {"pymt_txn_id": txn_id, "checkout_reference": f"RS-{txn_id[:8].upper()}", "amount": total_amount,
                "credit_amount": credit_amount, "balance": balance, "method": method}

    @classmethod
    async def save_payment_proof(cls, user_id: str, pymt_txn_id: str, method: str, reference_number: str,
                                 payer_name: Optional[str], file_name: Optional[str], content_type: str,
                                 proof_data: str) -> str:
        """Keeps the uploaded proof of payment for a pending transaction that belongs to this user."""
        import asyncio
        client = cls._client()

        def _save():
            txn = (client.table("pymt_txn").select("pymt_txn_id")
                   .eq("pymt_txn_id", pymt_txn_id).eq("user_id", user_id).execute())
            if not txn.data:
                raise ValueError(f"Payment transaction {pymt_txn_id} not found for user {user_id}.")
            proof_id = str(uuid.uuid4())
            client.table("payment_proof").insert({
                "proof_id": proof_id, "pymt_txn_id": pymt_txn_id, "user_id": user_id, "method": method,
                "reference_number": reference_number, "payer_name": payer_name, "file_name": file_name,
                "content_type": content_type, "proof_data": proof_data,
            }).execute()
            client.table("pymt_txn").update({"pymt_txn_provider": method}).eq("pymt_txn_id", pymt_txn_id).execute()
            return proof_id

        return await asyncio.to_thread(_save)

    @classmethod
    async def confirm_checkout(cls, user_id: str, pymt_txn_id: str) -> int:
        """Marks the transaction paid and credits the wallet via the
        purchase_credits RPC. This handler -- and only this handler -- is
        what a real payment webhook would replace; everything else in the
        credits flow is provider-agnostic."""
        import asyncio
        client = cls._client()

        def _fetch_txn():
            resp = (
                client.table("pymt_txn")
                .select("pymt_txn_id, user_id, pymt_txn_credits, pymt_txn_status")
                .eq("pymt_txn_id", pymt_txn_id)
                .eq("user_id", user_id)
                .execute()
            )
            return resp.data[0] if resp.data else None

        txn = await asyncio.to_thread(_fetch_txn)
        if not txn:
            raise ValueError(f"Payment transaction {pymt_txn_id} not found for user {user_id}.")
        if txn["pymt_txn_status"] == "paid":
            # Idempotent: confirming twice just returns the current balance.
            return await cls.get_balance(user_id)

        credit_amount = txn["pymt_txn_credits"]

        def _mark_paid():
            client.table("pymt_txn").update({"pymt_txn_status": "paid"}).eq("pymt_txn_id", pymt_txn_id).execute()

        await asyncio.to_thread(_mark_paid)

        def _call_purchase():
            return client.rpc("purchase_credits", {
                "p_user_id": user_id,
                "p_amount": credit_amount,
                "p_pymt_txn_id": pymt_txn_id,
                "p_idempotency_key": f"purchase:{pymt_txn_id}",
            }).execute()

        resp = await asyncio.to_thread(_call_purchase)
        return resp.data

    @classmethod
    async def get_ledger_history(cls, user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        import asyncio
        client = cls._client()

        def _fetch():
            resp = (
                client.table("credit_ledger")
                .select("ledger_id, kind, delta, balance_after, analysis_run_id, note, created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return resp.data or []

        return await asyncio.to_thread(_fetch)


credits_service = CreditsService()
