#!/usr/bin/env python3
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
                # Leer el UUID (viene en formato binario, 16 bytes)
                uuid_data = await reader.read(length)
                
                # Convertir bytes a UUID
                if len(uuid_data) == 16:
                    connection_uuid = str(uuid.UUID(bytes=uuid_data))
                else:
                    # Si no son 16 bytes, intentar como hex string
                    connection_uuid = uuid_data.hex()
                
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
        logger.info("🛑 Servidor detenido por usuario")