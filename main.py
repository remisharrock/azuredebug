#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import argparse
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from loguru import logger

from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection

# Load environment variables
load_dotenv(override=True)

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "null",  # Support for browser file:// requests
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:4321",
        "https://app.neocertif.com",
        "https://demo.neocertif.com",
        "https://voiceagent.neocertif.com",
        "https://ad.neocertif.com",
    ],
    allow_origin_regex=r"https?://192\.168\.1\.\d{1,3}(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Gestion des transports disponibles via la configuration .env


def get_enabled_transports() -> List[str]:
    raw_value = os.getenv("TRANSPORT_TYPE", "daily")
    transports = [item.strip().lower() for item in raw_value.split(",") if item.strip()]
    return transports or ["daily"]


def get_default_transport() -> str:
    transports = get_enabled_transports()
    return transports[0]


CLIENT_DIR = Path(__file__).parent / "client"

async def create_daily_room_and_token():
    """Create a Daily room and token for the bot session."""
    daily_api_key = os.getenv("DAILY_API_KEY")
    if not daily_api_key:
        raise HTTPException(status_code=500, detail="DAILY_API_KEY not configured")

    from pipecat.transports.daily.utils import DailyRESTHelper, DailyRoomParams, DailyRoomProperties

    async with aiohttp.ClientSession() as session:
        daily_helper = DailyRESTHelper(
            daily_api_key=daily_api_key,
            aiohttp_session=session
        )

        # Create room with 2-hour expiration
        room_params = DailyRoomParams(
            properties=DailyRoomProperties(
                exp=time.time() + 7200,  # 2 hours
                eject_at_room_exp=True,
                start_video_off=True,    # Audio-only by default
            )
        )

        room = await daily_helper.create_room(room_params)
        token = await daily_helper.get_token(room.url, expiry_time=7200)

        return room.url, token


@app.post("/connect")
async def connect(background_tasks: BackgroundTasks, request: Dict[str, Any] | None = None):
    """
    Endpoint pour créer une room Daily et lancer le bot
    """
    request_data = request or {}
    logger.info(f"Requête de connexion reçue: {request_data}")

    enabled_transports = get_enabled_transports()
    transport_type = request_data.get("transport_type", get_default_transport()).lower()

    if transport_type not in enabled_transports:
        logger.warning(f"Transport demandé non autorisé: {transport_type}")
        raise HTTPException(status_code=400, detail=f"Transport '{transport_type}' non disponible")

    if transport_type != "daily":
        raise HTTPException(status_code=400, detail="Utilisez l'endpoint /offer pour SmallWebRTC")

    try:
        room_url, token = await create_daily_room_and_token()
        logger.info(f"Room Daily créée: {room_url}")

        from pipecat.runner.types import DailyRunnerArguments

        runner_args = DailyRunnerArguments(
            room_url=room_url,
            token=token,
            body=request_data
        )

        from bot import bot

        background_tasks.add_task(bot, runner_args)

        return {
            "dailyRoom": room_url,
            "dailyToken": token,
            "message": "Room Daily créée avec succès, bot lancé"
        }

    except Exception as e:
        logger.error(f"Erreur lors de la création de la room Daily: {e}")
        raise HTTPException(status_code=500, detail=f"Échec de création de la room Daily: {str(e)}")


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "azuredebug-server",
        "enabled_transports": get_enabled_transports()
    }


@app.get("/")
async def serve_index():
    return {"message": "Azure Debug Voice AI Engine", "status": "running"}


@app.post("/offer")
async def offer(background_tasks: BackgroundTasks, request: Dict[str, Any]):
    """Endpoint SmallWebRTC pour gérer une offre WebRTC du client."""
    if "smallwebrtc" not in get_enabled_transports():
        raise HTTPException(status_code=400, detail="Le transport SmallWebRTC n'est pas activé")

    if "sdp" not in request or "type" not in request:
        raise HTTPException(status_code=400, detail="Offre SDP invalide")

    webrtc_connection = SmallWebRTCConnection()
    await webrtc_connection.initialize(sdp=request["sdp"], type=request["type"])

    from pipecat.runner.types import SmallWebRTCRunnerArguments
    from bot import bot

    runner_args = SmallWebRTCRunnerArguments(webrtc_connection=webrtc_connection)
    background_tasks.add_task(bot, runner_args)

    answer = webrtc_connection.get_answer()
    if not answer:
        raise HTTPException(status_code=500, detail="Impossible de créer une réponse WebRTC")

    return answer


@app.get("/client")
async def serve_client():
    client_file = CLIENT_DIR / "client.html"
    if not client_file.exists():
        raise HTTPException(status_code=404, detail="Client file not found")
    return FileResponse(client_file)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Serveur démarré - Prêt à recevoir des clients")
    yield
    logger.info("Arrêt du serveur")

# Mise à jour de l'app avec lifespan
app.router.lifespan_context = lifespan

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Azure Debug Voice AI Server")
    parser.add_argument(
        "--host", default="localhost", help="Host for HTTP server (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=7860, help="Port for HTTP server (default: 7860)"
    )
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    logger.remove(0)
    if args.verbose:
        logger.add(sys.stderr, level="DEBUG")
    else:
        logger.add(sys.stderr, level="INFO")

    uvicorn.run(app, host=args.host, port=args.port)
