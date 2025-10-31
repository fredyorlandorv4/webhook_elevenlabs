#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Servidor TCP que conecta Asterisk AudioSocket con ElevenLabs WebSocket directo
Ubicación: /opt/asterisk-elevenlabs-bridge/server.py
"""

import asyncio
import struct
import logging
import json
import uuid
import base64
import websockets

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


def resample_audio(audio_data, from_rate, to_rate):
    """
    Resamplea audio PCM 16-bit de from_rate a to_rate
    """
    if from_rate == to_rate:
        return audio_data
    
    # Número de samples (16-bit = 2 bytes por sample)
    num_samples = len(audio_data) // 2
    
    # Desempaquetar samples
    samples = struct.unpack(f'{num_samples}h', audio_data)
    
    # Calcular ratio de conversión
    ratio = from_rate / to_rate
    
    # Resamplear usando interpolación lineal simple
    output_samples = []
    output_length = int(num_samples / ratio)
    
    for i in range(output_length):
        # Posición en el audio original
        pos = i * ratio
        index = int(pos)
        frac = pos - index
        
        if index + 1 < len(samples):
            # Interpolación lineal entre dos samples
            sample = samples[index] * (1 - frac) + samples[index + 1] * frac
            output_samples.append(int(sample))
        elif index < len(samples):
            output_samples.append(samples[index])
    
    # Empaquetar de vuelta a bytes
    return struct.pack(f'{len(output_samples)}h', *output_samples)


class AudioSocketToElevenLabs:
    def __init__(self, api_key, agent_id):
        self.api_key = api_key
        self.agent_id = agent_id
        self.elevenlabs_ws = None
        self.signed_url = None
        
    async def get_signed_url(self):
        """
        Obtiene la URL firmada para conectarse al WebSocket de ElevenLabs
        """
        import aiohttp
        
        url = f"https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id={self.agent_id}"
        headers = {
            "xi-api-key": self.api_key
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    self.signed_url = data.get("signed_url")
                    logger.info(f"✅ URL firmada obtenida")
                    return self.signed_url
                else:
                    error = await response.text()
                    raise Exception(f"Error obteniendo URL: {response.status} - {error}")
    
    async def handle_asterisk_connection(self, reader, writer):
        """
        Maneja la conexión desde Asterisk
        """
        addr = writer.get_extra_info('peername')
        logger.info(f"🔌 Nueva conexión desde {addr}")
        
        try:
            # Leer el UUID del AudioSocket
            header = await reader.read(3)
            if len(header) < 3:
                logger.error("No se recibió header completo")
                return
            
            msg_type, length = struct.unpack('!BH', header)
            
            if msg_type == AUDIOSOCKET_UUID:
                uuid_data = await reader.read(length)
                if len(uuid_data) == 16:
                    connection_uuid = str(uuid.UUID(bytes=uuid_data))
                else:
                    connection_uuid = uuid_data.hex()
                
                logger.info(f"📞 UUID de conexión: {connection_uuid}")
                
                # Obtener URL firmada y conectar a ElevenLabs
                await self.get_signed_url()
                
                # Conectar al WebSocket de ElevenLabs
                async with websockets.connect(self.signed_url) as elevenlabs_ws:
                    self.elevenlabs_ws = elevenlabs_ws
                    logger.info("🤖 Conectado al WebSocket de ElevenLabs")
                    
                    # Procesar audio bidireccional
                    await self.bidirectional_audio(reader, writer, elevenlabs_ws)
            
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
        finally:
            writer.close()
            await writer.wait_closed()
            logger.info("✅ Conexión cerrada")
    
    async def bidirectional_audio(self, reader, writer, elevenlabs_ws):
        """
        Maneja el flujo de audio bidireccional
        """
        logger.info("🔄 Iniciando loop de audio bidireccional")
        
        # Tarea 1: Asterisk → ElevenLabs
        asterisk_to_eleven = asyncio.create_task(
            self.forward_asterisk_to_elevenlabs(reader, elevenlabs_ws)
        )
        logger.info("✅ Tarea Asterisk→ElevenLabs iniciada")
        
        # Tarea 2: ElevenLabs → Asterisk
        eleven_to_asterisk = asyncio.create_task(
            self.forward_elevenlabs_to_asterisk(elevenlabs_ws, writer)
        )
        logger.info("✅ Tarea ElevenLabs→Asterisk iniciada")
        
        logger.info("⏳ Esperando tareas de audio...")
        
        # Esperar a que cualquiera termine
        done, pending = await asyncio.wait(
            [asterisk_to_eleven, eleven_to_asterisk],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        logger.info(f"⚠️  Una tarea terminó. Cancelando pendientes...")
        
        # Cancelar tareas pendientes
        for task in pending:
            task.cancel()
            
        logger.info("✅ Loop de audio bidireccional finalizado")
    
    async def forward_asterisk_to_elevenlabs(self, reader, elevenlabs_ws):
        """
        Lee audio de Asterisk y lo envía a ElevenLabs
        """
        logger.info("🎤 Iniciando forward Asterisk→ElevenLabs")
        try:
            while True:
                # Leer header de AudioSocket
                header = await reader.read(3)
                if len(header) < 3:
                    logger.info("Conexión de Asterisk cerrada")
                    break
                
                msg_type, length = struct.unpack('!BH', header)
                
                if msg_type == AUDIOSOCKET_AUDIO:
                    # Leer audio
                    audio_data = await reader.read(length)
                    
                    if len(audio_data) > 0:
                        logger.debug(f"📥 Audio de Asterisk: {len(audio_data)} bytes (16kHz)")
                        
                        # Convertir de 16kHz a 8kHz (ElevenLabs espera 8kHz)
                        audio_8khz = resample_audio(audio_data, 16000, 8000)
                        
                        logger.debug(f"🔄 Convertido a 8kHz: {len(audio_8khz)} bytes")
                        
                        # Enviar a ElevenLabs
                        message = {
                            "user_audio_chunk": audio_base64
                        }
                        await elevenlabs_ws.send(json.dumps(message))
                        logger.debug("✅ Audio enviado a ElevenLabs")
                
                elif msg_type == AUDIOSOCKET_HANGUP:
                    logger.info("📞 Hangup de Asterisk")
                    break
                    
        except Exception as e:
            logger.error(f"Error en forward_asterisk_to_elevenlabs: {e}")
    
    async def forward_asterisk_to_elevenlabs(self, reader, elevenlabs_ws):
        """
        Lee audio de Asterisk y lo envía a ElevenLabs
        """
        logger.info("🎤 Iniciando forward Asterisk→ElevenLabs")
        try:
            while True:
                # Leer header de AudioSocket
                header = await reader.read(3)
                if len(header) < 3:
                    logger.info("Conexión de Asterisk cerrada")
                    break
                
                msg_type, length = struct.unpack('!BH', header)
                
                if msg_type == AUDIOSOCKET_AUDIO:
                    # Leer audio
                    audio_data = await reader.read(length)
                    
                    if len(audio_data) > 0:
                        logger.info(f"📥 Audio de Asterisk: {len(audio_data)} bytes (16kHz)")
                        
                        # Convertir de 16kHz a 8kHz
                        audio_8khz = resample_audio(audio_data, 16000, 8000)
                        logger.debug(f"🔄 Convertido a 8kHz: {len(audio_8khz)} bytes")
                        
                        # ESTA ES LA LÍNEA CRÍTICA - CONVERTIR A BASE64
                        audio_base64 = base64.b64encode(audio_8khz).decode('utf-8')
                        
                        # Enviar a ElevenLabs
                        message = {
                            "user_audio_chunk": audio_base64
                        }
                        await elevenlabs_ws.send(json.dumps(message))
                        logger.debug("✅ Audio enviado a ElevenLabs")
                
                elif msg_type == AUDIOSOCKET_HANGUP:
                    logger.info("📞 Hangup de Asterisk")
                    break
                    
        except Exception as e:
            logger.error(f"Error en forward_asterisk_to_elevenlabs: {e}")

async def main():
    """
    Inicia el servidor TCP
    """
    bridge = AudioSocketToElevenLabs(ELEVENLABS_API_KEY, AGENT_ID)
    
    server = await asyncio.start_server(
        bridge.handle_asterisk_connection,
        '0.0.0.0',
        TCP_PORT
    )
    
    addr = server.sockets[0].getsockname()
    logger.info(f"🚀 Servidor iniciado en {addr[0]}:{addr[1]}")
    logger.info(f"📞 Agente: {AGENT_ID}")
    logger.info("✅ Esperando conexiones...")
    
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Servidor detenido")