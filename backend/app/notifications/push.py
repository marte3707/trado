import asyncio
import json
from typing import Any

try:
    from pywebpush import WebPushException, webpush  # type: ignore[import-not-found]
except ImportError:
    # Fallback for environments where pywebpush is not installed
    class WebPushException(Exception):  # type: ignore[no-redef]
        pass
    def webpush(*args: Any, **kwargs: Any) -> Any:  # type: ignore[no-redef]
        pass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.models import Alert, PushSubscription


class WebPushNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send(self, db: Session, title: str, body: str, url: str = "/") -> int:
        if not self.settings.vapid_private_key:
            return 0
        subs = db.query(PushSubscription).filter(PushSubscription.active.is_(True)).all()
        if not subs:
            return 0
        data = json.dumps({"title": title, "body": body, "url": url, "tag": "trado"})
        loop = asyncio.get_event_loop()
        sent = 0
        def _do_push(s: PushSubscription) -> Any:
            return webpush(
                subscription_info={
                    "endpoint": s.endpoint,
                    "keys": {"p256dh": s.p256dh, "auth": s.auth},
                },
                data=data,
                vapid_private_key=self.settings.vapid_private_key,
                vapid_claims={"sub": self.settings.vapid_subject},
            )

        for sub in subs:
            try:
                await loop.run_in_executor(None, _do_push, sub)
                db.add(Alert(channel="browser_push", status="sent", payload={"endpoint": sub.endpoint[:60]}))
                sent += 1
            except WebPushException as e:
                if e.response is not None and e.response.status_code in (404, 410):
                    sub.active = False
                db.add(Alert(channel="browser_push", status="failed", payload={"error": str(e)[:200]}))
        db.commit()
        return sent
