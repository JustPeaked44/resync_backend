"""
Pay-per-scan credits ledger service.

Wraps the Supabase RPCs defined in migrations/004_credits.sql
(debit_scan_credit, refund_scan_credit, purchase_credits). All balance
mutation goes through those RPCs -- they are SECURITY DEFINER functions
with a conditional `WHERE balance >= 1` update, which is what makes two
concurrent scans against a balance of 1 resolve to exactly one success.
This service never mutates credit_wallet directly.

InsufficientCreditsError is the seam _provision_analysis_run in main.py
catches to return HTTP 402.
"""

import logging
import os
import uuid
from typing import Any, Dict, List, Optional

from services.db_service import DatabasePersistenceService

logger = logging.getLogger("resync.credits")

# ponytail: single env kill switch for capstone testing. CREDITS_ENABLED=false
# makes the wallet report unlimited and turns debit/refund into no-ops, so the
# scan pipeline never touches credit_wallet/credit_ledger. Flip back to true
# (or drop the var) to re-enable the real ledger.
CREDITS_ENABLED = os.getenv("CREDITS_ENABLED", "true").strip().lower() not in ("false", "0", "no")
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
