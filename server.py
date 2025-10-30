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
import queue
import threading
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuración
ELEVENLABS_API_KEY = "sk_c47af6d7d7d8ffe7997c0a0e9095a502f3e58f59f2f05e5f"
AGENT_ID = "agent_4401k5s1gcypecna6mt5p6przhqa"
TCP_PORT = 4449

# Constantes del protocolo AudioSocket
AUDIOSOCKET_UUID = 0x01
AUDIOSOCKET_AUDIO = 0x10
AUDIOSOCKET_HANGUP = 0x00


class CustomAudioInterface:
    """
    Audio Interface customizado para manejar audio desde/hacia Asterisk
    """
    def __init__(self, input_queue, output_queue):
        self.input_queue = input_queue  # Audio de Asterisk hacia ElevenLabs
        self.output_queue = output_queue  # Audio de ElevenLabs hacia Asterisk
        self.is_running = False
    
    def start(self, output_callback):
        """
        Inicia el audio interface
        """
        self.is_running = True
        self.output_callback = output_callback
        logger.info("CustomAudioInterface iniciado")
    
    def stop(self):
        """
        Detiene el audio interface
        """
        self.is_running = False
        logger.info("CustomAudioInterface detenido")
    
    def input(self, chunk):
        """
        ElevenLabs llama este método cuando necesita audio del usuario
        Retorna audio del input_queue
        """
        try:
            # Intentar obtener audio de la cola (no bloqueante)
            return self.input_queue.get(timeout=0.01)
        except queue.Empty:
            # Si no hay audio, retornar silencio
            return bytes(320)  # 20ms de silencio a 16kHz mono
    
    def output(self, chunk):
        """
        ElevenLabs llama este método cuando tiene audio para reproducir
        Lo ponemos en la cola para enviarlo a Asterisk
        """
        self.output_queue.put(chunk)
        
        # También llamar al callback si existe
        if hasattr(self, 'output_callback') and self.output_callback:
            self.output_callback(chunk)


class AudioSocketBridge:
    def __init__(self, api_key, agent_id):
        self.api_key = api_key
        self.agent_id = agent_id
        self.client = ElevenLabs(api_key=api_key)
        self.conversation = None
        self.audio_out_queue = queue.Queue()
        self.audio_in_queue = queue.Queue()
        
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
                    self.conversation.end_session()
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
        
        # Crear audio interface customizado
        audio_interface = CustomAudioInterface(self.audio_in_queue, self.audio_out_queue)
        
        self.conversation = Conversation(
            client=self.client,
            agent_id=self.agent_id,
            requires_auth=True,
            audio_interface=audio_interface,
            callback_agent_response=self.on_agent_response,
            callback_agent_response_correction=self.on_agent_response_correction,
            callback_user_transcript=self.on_user_transcript,
            callback_latency_measurement=self.on_latency_measurement,
        )
        
        # start_session() NO es async, se ejecuta en otro thread
        self.conversation.start_session()
        
        # Esperar un poco a que se conecte
        await asyncio.sleep(1)
        logger.info("✅ Sesión iniciada con ElevenLabs")
    
    async def audio_loop(self, reader, writer):
        """
        Loop principal que procesa audio bidireccional
        """
        self.writer = writer
        
        # Tarea para enviar audio del agente a Asterisk
        agent_task = asyncio.create_task(self.send_agent_audio_to_asterisk())
        
        
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
                        # Poner audio en la cola para ElevenLabs
                        logger.info(f"📥 Recibido audio de Asterisk: {len(audio_data)} bytes")
                        try:
                            self.audio_in_queue.put_nowait(audio_data)
                            logger.info(f"✅ Audio puesto en cola")
                        except queue.Full:
                            logger.warning("Cola de entrada llena, descartando audio")
                        
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
    
    async def send_agent_audio_to_asterisk(self):
        """
        Tarea que lee audio del agente desde la cola y lo envía a Asterisk
        """
        try:
            while True:
                # Intentar obtener audio de la cola
                try:
                    audio_chunk = self.audio_out_queue.get(timeout=0.01)
                    await self.send_audio_to_asterisk(audio_chunk)
                except queue.Empty:
                    # Si no hay audio, esperar un poco
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error en send_agent_audio_to_asterisk: {e}")
    
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