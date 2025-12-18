import discord
from discord.ext import commands
import asyncio
from typing import Optional
import traceback
import os
import sys

import sounddevice as sd
import numpy as np
import nacl

try:
    if not discord.opus.is_loaded():
        possible_paths = []
        base_dir = os.path.dirname(getattr(sys, "_MEIPASS", sys.argv[0]))
        possible_paths.append(os.path.join(base_dir, "opus.dll"))
        possible_paths.append(os.path.join(os.getcwd(), "opus.dll"))
        possible_paths.append("opus.dll")

        print("Trying to load Opus from these paths:")
        for p in possible_paths:
            print(f"  - {p}")
        for p in possible_paths:
            try:
                discord.opus.load_opus(p)
                print(f"Loaded Opus from: {p}")
                break
            except Exception as e:
                print(f"Failed to load Opus from {p}: {e!r}")
        else:
            print("WARNING: Could not load opus.dll. Voice will fail unless opus is available in the system.")
except Exception as _e:
    print(f"WARNING: Error while trying to load opus: {_e!r}")

DISCORD_TOKEN = 'paste your token'
VOICE_CHANNEL_ID = 'paste voice channel id'
CONTROL_CHANNEL_ID = 'paste text control channel'

CHUNK = 960
CHANNELS = 2
RATE = 48000


class MicRelayer:
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        intents.guilds = True

        self.bot = commands.Bot(command_prefix='!', intents=intents)
        self.voice_client: Optional[discord.VoiceClient] = None
        self.audio_stream: Optional[sd.InputStream] = None
        self.is_streaming = False
        self.input_devices = []
        self.current_device_idx: Optional[int] = None
        self.default_device_idx: Optional[int] = None
        
        @self.bot.event
        async def on_ready():
            print(f'Bot logged in as {self.bot.user}')
            try:
                control_ch = self.bot.get_channel(int(CONTROL_CHANNEL_ID))
                if control_ch:
                    await control_ch.send("Connected")
            except Exception:
                traceback.print_exc()
            await self.start_relay()

        @self.bot.event
        async def on_message(message: discord.Message):
            try:
                if message.author.bot:
                    return
                if str(message.channel.id) != CONTROL_CHANNEL_ID:
                    return

                content = message.content.strip()
                if content == "!list":
                    await self.handle_list_command(message)
                elif content.startswith("!change"):
                    parts = content.split()
                    if len(parts) == 2 and parts[1].isdigit():
                        idx = int(parts[1])
                        ok = await self.change_microphone(idx)
                        if ok:
                            await message.channel.send(f"swapped to {idx}")
                        else:
                            await message.channel.send("invalid index")
                elif content == "!close":
                    await self.shutdown()
            except Exception:
                traceback.print_exc()
    
    def refresh_input_devices(self):
        self.input_devices = []
        try:
            all_devices = sd.query_devices()
            default_input = sd.default.device[0]
        except Exception as e:
            print(f"Error querying devices: {e}")
            return

        for idx, dev in enumerate(all_devices):
            if dev.get("max_input_channels", 0) > 0:
                is_default = (idx == default_input)
                self.input_devices.append(
                    {"sd_index": idx, "is_default": is_default}
                )

        self.default_device_idx = None
        for i, dev in enumerate(self.input_devices, start=1):
            if dev["is_default"]:
                self.default_device_idx = i
                break
        if self.default_device_idx is None and self.input_devices:
            self.default_device_idx = 1

        if self.current_device_idx is None:
            self.current_device_idx = self.default_device_idx

    def create_audio_source(self, device_logical_index: Optional[int] = None):
        try:
            if self.audio_stream:
                try:
                    self.audio_stream.stop()
                    self.audio_stream.close()
                except Exception:
                    pass

            if not self.input_devices:
                self.refresh_input_devices()

            if device_logical_index is None:
                device_logical_index = self.current_device_idx or 1

            if device_logical_index < 1 or device_logical_index > len(self.input_devices):
                raise RuntimeError("Requested device index out of range")

            dev = self.input_devices[device_logical_index - 1]
            sd_index = dev["sd_index"]
            device_info = sd.query_devices(sd_index, "input")
            print(f"Using microphone index {device_logical_index} (SD index {sd_index}): {device_info['name']}")

            max_channels = int(device_info.get("max_input_channels", CHANNELS) or 1)
            use_channels = min(CHANNELS, max_channels)
            if use_channels < 1:
                raise RuntimeError("Selected device has no input channels")
            
            self.audio_stream = sd.InputStream(
                samplerate=RATE,
                channels=use_channels,
                dtype="int16",
                blocksize=CHUNK,
                device=sd_index,
            )
            self.audio_stream.start()
            
            class MicSource(discord.AudioSource):
                def __init__(self, stream: sd.InputStream):
                    self.stream = stream
                
                def read(self) -> bytes:
                    try:
                        data, overflowed = self.stream.read(CHUNK)
                        if overflowed:
                            print("Warning: Audio buffer overflow")
                        return data.tobytes()
                    except Exception as e:
                        print(f"Error reading audio: {e}")
                        return b'\x00' * (CHUNK * CHANNELS * 2)
                
                def cleanup(self):
                    try:
                        self.stream.stop()
                        self.stream.close()
                    except Exception:
                        pass
            
            return MicSource(self.audio_stream)
        except Exception as e:
            print(f"Error creating audio source: {e}")
            return None

    async def handle_list_command(self, message: discord.Message):
        self.refresh_input_devices()
        if not self.input_devices:
            await message.channel.send("no devices")
            return

        parts = []
        for i, dev in enumerate(self.input_devices, start=1):
            labels = []
            if self.default_device_idx == i:
                labels.append("default")
            if self.current_device_idx == i:
                labels.append("in use")
            label_str = ""
            if labels:
                label_str = " " + " ".join(f"({lbl})" for lbl in labels)
            parts.append(f"{i}{label_str}")

        await message.channel.send(",".join(parts))

    async def change_microphone(self, device_logical_index: int) -> bool:
        self.refresh_input_devices()
        if (
            not self.input_devices
            or device_logical_index < 1
            or device_logical_index > len(self.input_devices)
        ):
            return False
        if not self.voice_client or not self.voice_client.is_connected():
            return False

        audio_source = self.create_audio_source(device_logical_index)
        if not audio_source:
            return False

        try:
            if self.voice_client.is_playing():
                self.voice_client.stop()
            self.voice_client.play(audio_source)
            self.current_device_idx = device_logical_index
            print(f"Switched microphone to logical index {device_logical_index}")
            return True
        except Exception as e:
            print(f"Error switching microphone: {e}")
            traceback.print_exc()
            return False
    
    async def start_relay(self):
        if not DISCORD_TOKEN or not VOICE_CHANNEL_ID:
            print("ERROR: Configuration missing! Please edit the script.")
            await self.bot.close()
            return
        
        try:
            channel = self.bot.get_channel(int(VOICE_CHANNEL_ID))
            if not channel:
                print(f"ERROR: Voice channel with ID {VOICE_CHANNEL_ID} not found!")
                await self.bot.close()
                return
            
            print(f"Connecting to voice channel: {channel.name}")
            
            self.voice_client = await channel.connect()
            print("Connected to voice channel!")
            
            self.refresh_input_devices()

            audio_source = self.create_audio_source(self.current_device_idx)
            if not audio_source:
                print("ERROR: Could not create audio source!")
                await self.voice_client.disconnect()
                await self.bot.close()
                return
            
            self.voice_client.play(audio_source)
            self.is_streaming = True
            print("Microphone relay started! Audio is now streaming to Discord.")
            print("Press Ctrl+C to stop.")
            
        except discord.errors.ClientException as e:
            print(f"Discord error: {e!r}")
            traceback.print_exc()
            await self.bot.close()
        except Exception as e:
            print(f"Error starting relay: {e!r}")
            traceback.print_exc()
            await self.bot.close()
    
    async def cleanup(self):
        print("\nCleaning up...")
        self.is_streaming = False
        
        if self.voice_client:
            await self.voice_client.disconnect()
        
        if self.audio_stream:
            try:
                self.audio_stream.stop()
                self.audio_stream.close()
            except Exception:
                pass
        
        await self.bot.close()

    async def shutdown(self):
        try:
            await self.cleanup()
        finally:
            os._exit(0)
    
    def run(self):
        try:
            if not DISCORD_TOKEN or not VOICE_CHANNEL_ID:
                print("=" * 50)
                print("ERROR: Configuration missing!")
                print("=" * 50)
                print("Please edit mic_relayer.py and set:")
                print("  DISCORD_TOKEN = 'your_token_here'")
                print("  VOICE_CHANNEL_ID = 'your_channel_id'")
                print("=" * 50)
                input("Press Enter to exit...")
                return
            
            self.bot.run(DISCORD_TOKEN)
        except KeyboardInterrupt:
            print("\nStopping...")
        except Exception as e:
            print(f"Error: {e}")
        finally:
            asyncio.run(self.cleanup())

if __name__ == "__main__":
    relayer = MicRelayer()
    relayer.run()
    try:
        input("Press Enter to close this window...")
    except EOFError:
        pass

