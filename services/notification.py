import os
import json
import logging
import httpx
from typing import Optional, List, Dict, Any
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("resync.notification")


class NotificationDeliveryService:
    @classmethod
    def _get_supabase_client(cls) -> Client:
        """Helper to initialize and return Supabase client."""
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")
        if not supabase_url or not supabase_key:
            raise ValueError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY / SUPABASE_ANON_KEY must be set in environment.")
        return create_client(supabase_url, supabase_key)

    @classmethod
    async def send_scan_completion_alert(
        cls,
        user_id: str,
        manuscript_title: str,
        coherence_score: int,
        analysis_run_id: str
    ) -> bool:
        """
        1. Query `public.user_device_token` via Supabase to find device_onesignal_player_id for user_id.
        2. If player ID exists, send an async POST request to https://onesignal.com/api/v1/notifications:
           - Use ONESIGNAL_APP_ID and ONESIGNAL_REST_API_KEY from .env.
           - Headings: {"en": "Scan Complete!"}
           - Contents: {"en": f"Your manuscript '{manuscript_title}' scored {coherence_score}% coherence."}
        3. Insert an in-app notification record into `public.notification` table with notification_type='scan_completed'.
        4. Return True if sent/logged, False otherwise.
        """
        try:
            supabase = cls._get_supabase_client()

            # 1. Query public.user_device_token for player IDs associated with user_id
            player_ids: List[str] = []
            try:
                device_query = (
                    supabase.table("user_device_token")
                    .select("device_onesignal_player_id")
                    .eq("user_id", user_id)
                    .execute()
                )
                if device_query and device_query.data:
                    for row in device_query.data:
                        pid = row.get("device_onesignal_player_id")
                        if pid and isinstance(pid, str) and pid.strip():
                            player_ids.append(pid.strip())
            except Exception as e:
                logger.warning(f"Failed to query user_device_token for user {user_id}: {e}")

            # 2. If player ID exists, send OneSignal push notification
            onesignal_notification_id: Optional[str] = None
            onesignal_app_id = os.getenv("ONESIGNAL_APP_ID")
            onesignal_api_key = os.getenv("ONESIGNAL_REST_API_KEY")

            if player_ids and onesignal_app_id and onesignal_api_key:
                try:
                    headings = {"en": "Scan Complete!"}
                    contents = {"en": f"Your manuscript '{manuscript_title}' scored {coherence_score}% coherence."}
                    onesignal_payload: Dict[str, Any] = {
                        "app_id": onesignal_app_id,
                        "include_player_ids": player_ids,
                        "headings": headings,
                        "contents": contents,
                        "data": {
                            "analysis_run_id": str(analysis_run_id),
                            "coherence_score": coherence_score,
                            "manuscript_title": manuscript_title,
                        }
                    }
                    onesignal_headers = {
                        "Authorization": f"Basic {onesignal_api_key}",
                        "Content-Type": "application/json; charset=utf-8",
                        "Accept": "application/json",
                    }

                    async with httpx.AsyncClient(timeout=15.0) as client:
                        response = await client.post(
                            "https://onesignal.com/api/v1/notifications",
                            headers=onesignal_headers,
                            json=onesignal_payload,
                        )
                        if response.status_code in (200, 201):
                            res_json = response.json()
                            onesignal_notification_id = res_json.get("id")
                            logger.info(f"OneSignal push notification sent successfully: {onesignal_notification_id}")
                        else:
                            logger.warning(
                                f"OneSignal push failed with status {response.status_code}: {response.text}"
                            )
                except Exception as e:
                    logger.warning(f"Error sending OneSignal push notification: {e}")

            # 3. Insert in-app notification record into public.notification table
            notification_payload = json.dumps({
                "title": "Scan Complete!",
                "message": f"Your manuscript '{manuscript_title}' scored {coherence_score}% coherence.",
                "manuscript_title": manuscript_title,
                "coherence_score": coherence_score,
                "analysis_run_id": str(analysis_run_id),
            })

            notification_data: Dict[str, Any] = {
                "user_id": user_id,
                "notification_type": "scan_completed",
                "notification_payload": notification_payload,
                "notification_isread": False,
            }

            if analysis_run_id:
                notification_data["analysis_run_id"] = str(analysis_run_id)
            if onesignal_notification_id:
                notification_data["notification_onesignal_id"] = str(onesignal_notification_id)

            insert_response = supabase.table("notification").insert(notification_data).execute()

            if insert_response and insert_response.data:
                logger.info(f"In-app notification logged successfully for user {user_id}")
                return True
            else:
                logger.warning(f"Notification insertion returned empty data: {insert_response}")
                return False

        except Exception as e:
            logger.error(f"Error in send_scan_completion_alert: {e}", exc_info=True)
            return False


# Global instance for convenient import
notification_service = NotificationDeliveryService()
