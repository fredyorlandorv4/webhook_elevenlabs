#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor que conecta Asterisk AudioSocket con ElevenLabs usando AudioInterface
BASADO EN EL PATRÓN CORRECTO DE TWILIO
"""

import asyncio
import struct
import logging
import uuid
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation, AudioInterface

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuración
ELEVENLABS_API_KEY = "sk_c47af6d7d7d8ffe7997c0a0e9095a502f3e58f59f2f05e5f"
AGENT_ID = "agent_4401k5s1gcypecna6mt5p6przhqa"
TCP_PORT = 4449

# Constantes AudioSocket
AUDIOSOCKET_UUID = 0x01
AUDIOSOCKET_AUDIO = 0x10
AUDIOSOCKET_HANGUP = 0x00


class AudioSocketInterface(AudioInterface):
    """
    AudioInterface customizado para AudioSocket (similar a TwilioAudioInterface)
    """
    def __init__(self, reader, writer, loop):
        self.reader = reader
        self.writer = writer
        self.input_callback = None
        self.loop = loop  # Guardar referencia al event loop
        self.is_running = False
        self._read_task = None
        
    def start(self, input_callback):
        """
        ElevenLabs llama este método para iniciar el audio interface
        input_callback: función que ElevenLabs llama cuando necesita audio del usuario
        """
        logger.info("🎤 AudioInterface iniciado")
        self.input_callback = input_callback
        self.is_running = True
        
        # Iniciar tarea para leer audio de Asterisk usando run_coroutine_threadsafe
        # porque start() se llama desde otro thread
        self._read_task = asyncio.run_coroutine_threadsafe(
            self._read_from_asterisk(),
            self.loop
        )
    
    def stop(self):
        """
        ElevenLabs llama este método para detener el audio interface
        """
        logger.info("🛑 AudioInterface detenido")
        self.is_running = False
        self.input_callback = None
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
    
    def output(self, audio: bytes):
        """
        ElevenLabs llama este método cuando tiene audio para enviar
        Este método debe retornar rápido y no bloquear
        """
        logger.debug(f"📤 ElevenLabs envió audio: {len(audio)} bytes")
        # Programar el envío en el event loop
        asyncio.run_coroutine_threadsafe(
            self._send_to_asterisk(audio),
            self.loop
        )
    
    def interrupt(self):
        """
        ElevenLabs llama este método para interrumpir el audio actual
        """
        logger.debug("⏸️  Interrupción solicitada")
        # En AudioSocket no hay un mecanismo de interrupción específico
        pass
    
    async def _read_from_asterisk(self):
        """
        Lee audio de Asterisk y lo pasa a ElevenLabs via input_callback
        """
        logger.info("👂 Iniciando lectura de audio desde Asterisk")
        try:
            while self.is_running:
                # Leer header de AudioSocket (3 bytes)
                header = await self.reader.read(3)
                if len(header) < 3:
                    logger.info("Conexión de Asterisk cerrada")
                    break
                
                msg_type, length = struct.unpack('!BH', header)
                
                if msg_type == AUDIOSOCKET_AUDIO:
                    # Leer datos de audio
                    audio_data = await self.reader.read(length)
                    
                    if len(audio_data) > 0 and self.input_callback:
                        logger.debug(f"📥 Audio de Asterisk: {len(audio_data)} bytes")
                        # Enviar a ElevenLabs
                        # AudioSocket envía PCM 16-bit 16kHz mono - formato correcto
                        self.input_callback(audio_data)
                
                elif msg_type == AUDIOSOCKET_HANGUP:
                    logger.info("📞 Hangup recibido de Asterisk")
                    self.is_running = False
                    break
                    
        except asyncio.CancelledError:
            logger.info("Tarea de lectura cancelada")
        except Exception as e:
            logger.error(f"Error leyendo de Asterisk: {e}", exc_info=True)
    
    async def _send_to_asterisk(self, audio_data: bytes):
        """
        Envía audio a Asterisk usando el protocolo AudioSocket
        """
        try:
            logger.debug(f"✉️  Enviando {len(audio_data)} bytes a Asterisk")
            
            # Crear paquete AudioSocket: [tipo(1byte), longitud(2bytes), datos]
            header = struct.pack('!BH', AUDIOSOCKET_AUDIO, len(audio_data))
            packet = header + audio_data
            
            self.writer.write(packet)
            await self.writer.drain()
            
            logger.debug("✅ Audio enviado a Asterisk")
            
        except Exception as e:
            logger.error(f"Error enviando audio a Asterisk: {e}")


class AudioSocketServer:
    def __init__(self, api_key, agent_id):
        self.api_key = api_key
        self.agent_id = agent_id
        self.client = ElevenLabs(api_key=api_key)
    
    async def handle_connection(self, reader, writer):
        """
        Maneja una conexión AudioSocket desde Asterisk
        """
        addr = writer.get_extra_info('peername')
        logger.info(f"🔌 Nueva conexión desde {addr}")
        
        conversation = None
        audio_interface = None
        
        try:
            # Leer el UUID del AudioSocket
            header = await reader.read(3)
            if len(header) < 3:
                logger.error("No se recibió header completo")
                return
            
            msg_type, length = struct.unpack('!BH', header)
            
            if msg_type == AUDIOSOCKET_UUID:
                # Leer UUID
                uuid_data = await reader.read(length)
                if len(uuid_data) == 16:
                    connection_uuid = str(uuid.UUID(bytes=uuid_data))
                else:
                    connection_uuid = uuid_data.hex()
                
                logger.info(f"📞 UUID de conexión: {connection_uuid}")
                
                # Obtener el event loop actual
                loop = asyncio.get_running_loop()
                
                # Crear el AudioInterface pasándole el loop
                audio_interface = AudioSocketInterface(reader, writer, loop)
                
                # Crear la conversación con ElevenLabs
                logger.info(f"🤖 Iniciando conversación con agente {self.agent_id}")
                
                conversation = Conversation(
                    client=self.client,
                    agent_id=self.agent_id,
                    requires_auth=True,
                    audio_interface=audio_interface,
                    callback_agent_response=lambda text: logger.info(f"🤖 Agente: {text}"),
                    callback_user_transcript=lambda text: logger.info(f"👤 Usuario: {text}"),
                    callback_agent_response_correction=lambda text: logger.debug(f"Corrección: {text}"),
                    callback_latency_measurement=lambda latency: logger.debug(f"Latencia: {latency}ms"),
                )
                
                # Iniciar la sesión (esto es síncrono, corre en otro thread)
                conversation.start_session()
                logger.info("✅ Conversación iniciada")
                
                # Esperar a que el AudioInterface se inicie (start() se llama desde otro thread)
                timeout = 5  # 5 segundos de timeout
                for _ in range(timeout * 10):
                    if audio_interface.is_running:
                        logger.info("🎧 AudioInterface activo y escuchando")
                        break
                    await asyncio.sleep(0.1)
                else:
                    logger.error("⚠️  Timeout esperando que AudioInterface se inicie")
                    return
                
                # Mantener la conexión mientras el audio_interface está activo
                while audio_interface.is_running:
                    await asyncio.sleep(0.1)
                
                logger.info("Conversación terminada")
            
        except Exception as e:
            logger.error(f"Error en handle_connection: {e}", exc_info=True)
        finally:
            # Limpiar
            if conversation:
                try:
                    conversation.end_session()
                    logger.info("Sesión de ElevenLabs terminada")
                except Exception as e:
                    logger.error(f"Error terminando sesión: {e}")
            
            if audio_interface:
                audio_interface.stop()
            
            writer.close()
            await writer.wait_closed()
            logger.info("✅ Conexión cerrada")


async def main():
    """
    Inicia el servidor TCP
    """
    server_instance = AudioSocketServer(ELEVENLABS_API_KEY, AGENT_ID)
    
    server = await asyncio.start_server(
        server_instance.handle_connection,
        '0.0.0.0',
        TCP_PORT
    )
    
    addr = server.sockets[0].getsockname()
    logger.info(f"🚀 Servidor iniciado en {addr[0]}:{addr[1]}")
    logger.info(f"📞 Agente ElevenLabs: {AGENT_ID}")
    logger.info("✅ Esperando conexiones desde Asterisk...")
    
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Servidor detenido por usuario")