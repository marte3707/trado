from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.logging import configure_logging
from app.db.init_db import create_schema, seed_defaults
from app.db.session import SessionLocal

configure_logging()

app = FastAPI(title="Trado API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://*.vercel.app"],
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)


@app.on_event("startup")
def startup() -> None:
    create_schema()
    db = SessionLocal()
    try:
        seed_defaults(db)
    finally:
        db.close()
