#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor que conecta Asterisk con ElevenLabs Conversational AI
Ubicación: /opt/asterisk-elevenlabs-bridge/server.py
"""

import asyncio
import websockets
import json
import base64
import struct
import logging
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuración
ELEVENLABS_API_KEY = "TU_API_KEY_AQUI"
AGENT_ID = "TU_AGENT_ID_AQUI"
WEBSOCKET_PORT = 4449

class AudioBridge:
    def __init__(self, api_key, agent_id):
        self.api_key = api_key
        self.agent_id = agent_id
        self.client = ElevenLabs(api_key=api_key)
        
    async def handle_connection(self, websocket, path):
        """
        Maneja conexión desde Asterisk usando protocolo AudioSocket
        """
        logger.info(f"Nueva conexión desde {websocket.remote_address}")
        
        try:
            # Iniciar conversación con ElevenLabs
            conversation = Conversation(
                agent_id=self.agent_id,
                callback_agent_response=self.on_agent_response,
                callback_agent_response_correction=self.on_agent_response_correction,
                callback_user_transcript=self.on_user_transcript,
                callback_latency_measurement=self.on_latency_measurement,
            )
            
            await conversation.start_session()
            logger.info(f"Sesión iniciada con agente {self.agent_id}")
            
            # Variables para manejar el estado
            self.websocket = websocket
            self.conversation = conversation
            
            # Procesar audio desde Asterisk
            async for message in websocket:
                if isinstance(message, bytes):
                    # AudioSocket Protocol: primeros 3 bytes son header
                    if len(message) > 3:
                        # Parsear el header de AudioSocket
                        msg_type = message[0]
                        
                        if msg_type == 0x10:  # Audio data
                            # Extraer el audio (después del header)
                            audio_data = message[3:]
                            
                            # Convertir de slin (signed linear) a formato para ElevenLabs
                            # AudioSocket envía audio en formato slin16 (16-bit PCM, 16kHz)
                            pcm_audio = self.convert_slin_to_pcm(audio_data)
                            
                            # Enviar al agente de ElevenLabs
                            await conversation.send_audio(pcm_audio)
                            
        except websockets.exceptions.ConnectionClosed:
            logger.info("Conexión cerrada por Asterisk")
        except Exception as e:
            logger.error(f"Error en handle_connection: {e}", exc_info=True)
        finally:
            if hasattr(self, 'conversation'):
                await conversation.end_session()
            logger.info("Sesión terminada")
    
    def convert_slin_to_pcm(self, slin_data):
        """
        Convierte audio slin16 a PCM para ElevenLabs
        """
        # slin16 ya es PCM 16-bit signed little-endian
        return slin_data
    
    def convert_pcm_to_slin(self, pcm_data):
        """
        Convierte PCM de ElevenLabs a formato slin16 para Asterisk
        """
        return pcm_data
    
    async def on_agent_response(self, response):
        """
        Callback cuando el agente responde con audio
        """
        try:
            # El audio viene en base64 o bytes
            if isinstance(response, str):
                audio_data = base64.b64decode(response)
            else:
                audio_data = response
            
            # Convertir y enviar a Asterisk
            slin_audio = self.convert_pcm_to_slin(audio_data)
            
            # Crear paquete AudioSocket con header
            # Header: [tipo(1byte), longitud(2bytes)]
            audio_length = len(slin_audio)
            header = struct.pack('!BH', 0x10, audio_length)
            packet = header + slin_audio
            
            await self.websocket.send(packet)
            
        except Exception as e:
            logger.error(f"Error en on_agent_response: {e}")
    
    async def on_agent_response_correction(self, correction):
        """Callback para correcciones del agente"""
        logger.debug(f"Corrección: {correction}")
    
    async def on_user_transcript(self, transcript):
        """Callback cuando se transcribe lo que dice el usuario"""
        logger.info(f"Usuario dijo: {transcript}")
    
    async def on_latency_measurement(self, latency):
        """Callback para métricas de latencia"""
        logger.debug(f"Latencia: {latency}ms")


async def main():
    """
    Inicia el servidor WebSocket
    """
    bridge = AudioBridge(ELEVENLABS_API_KEY, AGENT_ID)
    
    # Iniciar servidor WebSocket
    server = await websockets.serve(
        bridge.handle_connection,
        "0.0.0.0",
        WEBSOCKET_PORT,
        # Configuración para manejar conexiones largas
        ping_interval=20,
        ping_timeout=20,
        max_size=10 * 1024 * 1024  # 10MB max message size
    )
    
    logger.info(f"🚀 Servidor iniciado en ws://0.0.0.0:{WEBSOCKET_PORT}")
    logger.info(f"📞 Listo para conectar con agente {AGENT_ID}")
    logger.info("✅ Esperando conexiones desde Asterisk...")
    
    # Mantener el servidor corriendo
    await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Servidor detenido por usuario")