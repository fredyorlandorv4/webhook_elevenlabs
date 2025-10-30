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
from elevenlabs.conversa#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor TCP que maneja el protocolo AudioSocket de Asterisk y lo conecta con ElevenLabs
Ubicación: /opt/asterisk-elevenlabs-bridge/server.py
"""

import asyncio
import struct
import logging
import json
import uuid
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
TCP_PORT = 4449

# Constantes del protocolo AudioSocket
AUDIOSOCKET_UUID = 0x01
AUDIOSOCKET_AUDIO = 0x10
AUDIOSOCKET_HANGUP = 0x00

class AudioSocketBridge:
    def __init__(self, api_key, agent_id):
        self.api_key = api_key
        self.agent_id = agent_id
        self.client = ElevenLabs(api_key=api_key)
        self.conversation = None
        
    async def handle_connection(self, reader, writer):
        """
        Maneja conexión desde Asterisk usando protocolo AudioSocket
        """
        addr = writer.get_extra_info('peername')
        logger.info(f"🔌 Nueva conexión desde {addr}")
        
        try:
            # Leer el primer mensaje (debería ser UUID)
            header = await reader.read(3)
            if len(header) < 3:
                logger.error("No se recibió header completo")
                return
            
            msg_type, length = struct.unpack('!BH', header)
            logger.info(f"Tipo de mensaje: {hex(msg_type)}, Longitud: {length}")
            
            if msg_type == AUDIOSOCKET_UUID:
                # Leer el UUID
                uuid_data = await reader.read(length)
                connection_uuid = uuid_data.decode('utf-8')
                logger.info(f"📞 UUID de conexión: {connection_uuid}")
                
                # Iniciar conversación con ElevenLabs
                await self.start_elevenlabs_conversation()
                
                # Procesar audio en loop
                await self.audio_loop(reader, writer)
            
        except asyncio.CancelledError:
            logger.info("Conexión cancelada")
        except Exception as e:
            logger.error(f"Error en handle_connection: {e}", exc_info=True)
        finally:
            if self.conversation:
                try:
                    await self.conversation.end_session()
                except:
                    pass
            writer.close()
            await writer.wait_closed()
            logger.info("✅ Conexión cerrada")
    
    async def start_elevenlabs_conversation(self):
        """
        Inicia la sesión con ElevenLabs Conversational AI
        """
        logger.info(f"🤖 Iniciando conversación con agente {self.agent_id}")
        
        self.conversation = Conversation(
            agent_id=self.agent_id,
            callback_agent_response=self.on_agent_response,
            callback_agent_response_correction=self.on_agent_response_correction,
            callback_user_transcript=self.on_user_transcript,
            callback_latency_measurement=self.on_latency_measurement,
        )
        
        await self.conversation.start_session()
        logger.info("✅ Sesión iniciada con ElevenLabs")
    
    async def audio_loop(self, reader, writer):
        """
        Loop principal que procesa audio bidireccional
        """
        self.writer = writer
        
        # Tarea para recibir audio del agente
        agent_task = asyncio.create_task(self.receive_from_agent())
        
        try:
            while True:
                # Leer header del AudioSocket (3 bytes)
                header = await reader.read(3)
                if len(header) < 3:
                    logger.info("Conexión cerrada por Asterisk")
                    break
                
                msg_type, length = struct.unpack('!BH', header)
                
                if msg_type == AUDIOSOCKET_AUDIO:
                    # Leer datos de audio
                    audio_data = await reader.read(length)
                    
                    if len(audio_data) > 0:
                        # Enviar audio a ElevenLabs
                        await self.conversation.send_audio(audio_data)
                        
                elif msg_type == AUDIOSOCKET_HANGUP:
                    logger.info("📞 Hangup recibido")
                    break
                else:
                    logger.warning(f"Tipo de mensaje desconocido: {hex(msg_type)}")
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error en audio_loop: {e}", exc_info=True)
        finally:
            agent_task.cancel()
    
    async def receive_from_agent(self):
        """
        Recibe audio del agente de ElevenLabs y lo envía a Asterisk
        """
        try:
            async for audio_chunk in self.conversation.receive_audio():
                # Enviar a Asterisk usando el protocolo AudioSocket
                await self.send_audio_to_asterisk(audio_chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error en receive_from_agent: {e}")
    
    async def send_audio_to_asterisk(self, audio_data):
        """
        Envía audio a Asterisk usando el protocolo AudioSocket
        """
        try:
            # Crear header: [tipo(1byte), longitud(2bytes)]
            audio_length = len(audio_data)
            header = struct.pack('!BH', AUDIOSOCKET_AUDIO, audio_length)
            packet = header + audio_data
            
            self.writer.write(packet)
            await self.writer.drain()
            
        except Exception as e:
            logger.error(f"Error enviando audio a Asterisk: {e}")
    
    async def on_agent_response(self, response):
        """Callback cuando el agente responde"""
        logger.debug("🎤 Agente respondiendo...")
    
    async def on_agent_response_correction(self, correction):
        """Callback para correcciones"""
        logger.debug(f"Corrección: {correction}")
    
    async def on_user_transcript(self, transcript):
        """Callback cuando se transcribe lo que dice el usuario"""
        logger.info(f"👤 Usuario: {transcript}")
    
    async def on_latency_measurement(self, latency):
        """Callback para métricas de latencia"""
        logger.debug(f"⏱️  Latencia: {latency}ms")


async def main():
    """
    Inicia el servidor TCP para AudioSocket
    """
    bridge = AudioSocketBridge(ELEVENLABS_API_KEY, AGENT_ID)
    
    # Iniciar servidor TCP
    server = await asyncio.start_server(
        bridge.handle_connection,
        '0.0.0.0',
        TCP_PORT
    )
    
    addr = server.sockets[0].getsockname()
    logger.info(f"🚀 Servidor AudioSocket iniciado en {addr[0]}:{addr[1]}")
    logger.info(f"📞 Listo para conectar con agente {AGENT_ID}")
    logger.info("✅ Esperando conexiones desde Asterisk...")
    
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Servidor detenido por usuario")tional_ai.conversation import Conversation

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