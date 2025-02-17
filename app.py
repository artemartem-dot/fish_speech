import os
os.environ["TORCH_COMPILE"] = "0"
os.environ["TORCHDYNAMO_DISABLE"] = "1"
root_dir = os.path.dirname(os.path.abspath(__file__))
outputs_dir = os.path.join(root_dir, "..", "outputs")
os.makedirs(outputs_dir, exist_ok=True)
os.environ["GRADIO_TEMP_DIR"] = outputs_dir

import queue
from huggingface_hub import snapshot_download
import hydra
import numpy as np
import wave
import io
import pyrootutils
import gc
from datetime import datetime

# Download if not exists
os.makedirs("checkpoints", exist_ok=True)
snapshot_download(repo_id="fishaudio/fish-speech-1.5", local_dir="./checkpoints/fish-speech-1.5")

print("All checkpoints downloaded")

import html
import os
import threading
from argparse import ArgumentParser
from pathlib import Path
from functools import partial

import gradio as gr
import librosa
import torch
import torchaudio

torchaudio.set_audio_backend("soundfile")

from loguru import logger
from transformers import AutoTokenizer

from fish_speech.i18n import i18n
from fish_speech.text.chn_text_norm.text import Text as ChnNormedText
from fish_speech.utils import autocast_exclude_mps, set_seed
from tools.api import decode_vq_tokens, encode_reference
from tools.file import AUDIO_EXTENSIONS, list_files
from tools.llama.generate import (
    GenerateRequest,
    GenerateResponse,
    WrappedGenerateResponse,
    launch_thread_safe_queue,
)
from tools.vqgan.inference import load_model as load_decoder_model

from tools.schema import (
    GLOBAL_NUM_SAMPLES,
    ASRPackRequest,
    ServeASRRequest,
    ServeASRResponse,
    ServeASRSegment,
    ServeAudioPart,
    ServeForwardMessage,
    ServeMessage,
    ServeRequest,
    ServeResponse,
    ServeStreamDelta,
    ServeStreamResponse,
    ServeTextPart,
    ServeTimedASRResponse,
    ServeTTSRequest,
    ServeVQGANDecodeRequest,
    ServeVQGANDecodeResponse,
    ServeVQGANEncodeRequest,
    ServeVQGANEncodeResponse,
    ServeVQPart,
    ServeReferenceAudio
)

def wav_chunk_header(sample_rate=44100, bit_depth=16, channels=1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(bit_depth // 8)
        wav_file.setframerate(sample_rate)
    wav_header_bytes = buffer.getvalue()
    buffer.close()
    return wav_header_bytes

HEADER_MD = """# Fish Speech
## Fish Speech - нейросеть для преобразования текста в речь. GitHub: https://github.com/fishaudio/fish-speech  
### Портативная версия от 👾 НЕЙРО-СОФТ https://t.me/neuroport
"""

TEXTBOX_PLACEHOLDER = """Введите ваш текст здесь."""

try:
    import spaces
    GPU_DECORATOR = spaces.GPU
except ImportError:
    def GPU_DECORATOR(func):
        def wrapper(*args, **kwargs):
            with torch.inference_mode():
                return func(*args, **kwargs)
        return wrapper

def build_html_error_message(error):
    return f"""
    <div style="color: red; 
    font-weight: bold;">
        {html.escape(str(error))}
    </div>
    """

@GPU_DECORATOR
@torch.inference_mode()
def inference(req: ServeTTSRequest, selected_formats):
    refs = req.references
    prompt_tokens = [
        encode_reference(
            decoder_model=decoder_model,
            reference_audio=ref.audio,
            enable_reference_audio=True,
        )
        for ref in refs
    ]
    prompt_texts = [ref.text for ref in refs]

    if req.seed is not None:
        set_seed(req.seed)
        logger.warning(f"set seed: {req.seed}")

    request = dict(
        device=decoder_model.device,
        max_new_tokens=req.max_new_tokens,
        text=req.text,
        top_p=req.top_p,
        repetition_penalty=req.repetition_penalty,
        temperature=req.temperature,
        compile=args.compile,
        iterative_prompt=req.chunk_length > 0,
        chunk_length=req.chunk_length,
        max_length=4096,
        prompt_tokens=prompt_tokens,
        prompt_text=prompt_texts,
    )

    response_queue = queue.Queue()
    llama_queue.put(
        GenerateRequest(
            request=request,
            response_queue=response_queue,
        )
    )

    segments = []

    while True:
        result: WrappedGenerateResponse = response_queue.get()
        if result.status == "error":
            yield None, None, build_html_error_message(result.response)
            break

        result: GenerateResponse = result.response
        if result.action == "next":
            break

        with autocast_exclude_mps(
            device_type=decoder_model.device.type, dtype=args.precision
        ):
            fake_audios = decode_vq_tokens(
                decoder_model=decoder_model,
                codes=result.codes,
            )

        fake_audios = fake_audios.float().cpu().numpy()
        segments.append(fake_audios)

    if len(segments) == 0:
        return (
            None,
            None,
            build_html_error_message(
                i18n("Аудио не сгенерировано, пожалуйста, проверьте входной текст.")
            ),
        )

    audio = np.concatenate(segments, axis=0)
    
    # Конвертируем numpy array в тензор для torchaudio
    audio_tensor = torch.from_numpy(audio).unsqueeze(0)
    
    # Словарь для хранения путей к файлам
    audio_paths = {fmt: None for fmt in ['wav', 'mp3', 'flac']}
    
    # Сохраняем только выбранные форматы
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    for fmt in selected_formats:
        path = os.path.join(outputs_dir, f"output_{timestamp}.{fmt}")
        torchaudio.save(path, audio_tensor, decoder_model.spec_transform.sample_rate)
        audio_paths[fmt] = path

    # Возвращаем пути и базовое аудио для предпросмотра
    yield (None, (decoder_model.spec_transform.sample_rate, audio), None, 
           audio_paths['wav'], audio_paths['mp3'], audio_paths['flac'])

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        gc.collect()

def inference_wrapper(
    text,
    reference_audio,
    reference_text,
    max_new_tokens,
    chunk_length,
    top_p,
    repetition_penalty,
    temperature,
    seed,
    wav_format,
    mp3_format,
    flac_format,
):
    # Собираем список выбранных форматов
    selected_formats = []
    if wav_format:
        selected_formats.append('wav')
    if mp3_format:
        selected_formats.append('mp3')
    if flac_format:
        selected_formats.append('flac')
    
    # Если ничего не выбрано, используем WAV по умолчанию
    if not selected_formats:
        selected_formats = ['wav']

    references = []
    if reference_audio:
        with open(reference_audio, 'rb') as audio_file:
            audio_bytes = audio_file.read()
        references = [
            ServeReferenceAudio(audio=audio_bytes, text=reference_text)
        ]

    req = ServeTTSRequest(
        text=text,
        normalize=False,
        reference_id=None,
        references=references,
        max_new_tokens=max_new_tokens,
        chunk_length=chunk_length,
        top_p=top_p,
        repetition_penalty=repetition_penalty,
        temperature=temperature,
        seed=int(seed) if seed else None,
        use_memory_cache="never",
    )
    
    for result in inference(req, selected_formats):
        preview_audio, preview_sr_and_data, error_msg, wav_path, mp3_path, flac_path = result
        
        # Подготавливаем выходные значения
        outputs = []
        
        # Если есть WAV и он был выбран
        if 'wav' in selected_formats:
            outputs.append(wav_path)
        else:
            outputs.append(None)
            
        # Если есть MP3 и он был выбран
        if 'mp3' in selected_formats:
            outputs.append(mp3_path)
        else:
            outputs.append(None)
            
        # Если есть FLAC и он был выбран
        if 'flac' in selected_formats:
            outputs.append(flac_path)
        else:
            outputs.append(None)
            
        outputs.append(error_msg)  # Добавляем сообщение об ошибке
        
        return outputs

def build_app():
    with gr.Blocks(theme=gr.themes.Base()) as app:
        gr.Markdown(HEADER_MD)

        app.load(
            None,
            None,
            js="() => {const params = new URLSearchParams(window.location.search);if (!params.has('__theme')) {params.set('__theme', 'dark');window.location.search = params.toString();}}"
        )

        with gr.Row():
            with gr.Column(scale=3):
                text = gr.Textbox(
                    label="Входной текст", 
                    placeholder=TEXTBOX_PLACEHOLDER,
                    lines=10
                )

                with gr.Row():
                    with gr.Column():
                        with gr.Tab(label="Расширенные настройки"):
                            with gr.Row():
                                chunk_length = gr.Slider(
                                    label="Длина итеративного промпта, 0 означает выключено",
                                    minimum=0,
                                    maximum=300,
                                    value=200,
                                    step=8,
                                )

                                max_new_tokens = gr.Slider(
                                    label="Максимальное количество токенов в пакете",
                                    minimum=512,
                                    maximum=2048,
                                    value=1024,
                                    step=64,
                                )

                            with gr.Row():
                                top_p = gr.Slider(
                                    label="Top-P",
                                    minimum=0.6,
                                    maximum=0.9,
                                    value=0.7,
                                    step=0.01,
                                )

                                repetition_penalty = gr.Slider(
                                    label="Штраф за повторение",
                                    minimum=1,
                                    maximum=1.5,
                                    value=1.2,
                                    step=0.01,
                                )

                            with gr.Row():
                                temperature = gr.Slider(
                                    label="Температура",
                                    minimum=0.6,
                                    maximum=0.9,
                                    value=0.7,
                                    step=0.01,
                                )
                                seed = gr.Number(
                                    label="Сид",
                                    info="0 означает случайную генерацию, иначе - детерминированную",
                                    value=0,
                                )
                            
                            # Добавляем выбор форматов
                            with gr.Row():
                                gr.Markdown("### Форматы сохранения")
                            with gr.Row():
                                wav_format = gr.Checkbox(label="WAV", value=True)
                                mp3_format = gr.Checkbox(label="MP3", value=False)
                                flac_format = gr.Checkbox(label="FLAC", value=False)

                        with gr.Tab(label="Аудио для референса"):
                            with gr.Row():
                                gr.Markdown(
                                    "15-60 секунд референсного аудио, полезно для указания голоса говорящего."
                                )

                            with gr.Row():
                                example_audio_files = [f for f in os.listdir("examples") if f.lower().endswith(('.wav', '.mp3'))]
                                example_audio_dropdown = gr.Dropdown(
                                    label="Выберите пример аудио",
                                    choices=[""] + example_audio_files,
                                    value=""
                                )

                            with gr.Row():
                                reference_audio = gr.Audio(
                                    label="Референсное аудио",
                                    type="filepath",
                                )

                            with gr.Row():
                                reference_text = gr.Textbox(
                                    label="Референсный текст",
                                    lines=1,
                                    placeholder="В неведении день во сне закончился, и новый «цикл» начнется.",
                                    value="",
                                )

            with gr.Column(scale=3):
                with gr.Row():
                    error = gr.HTML(
                        label="Сообщение об ошибке",
                        visible=True,
                    )
                with gr.Row():
                    audio_wav = gr.Audio(
                        label="WAV",
                        type="filepath",
                        interactive=False,
                        visible=True,
                    )
                    audio_mp3 = gr.Audio(
                        label="MP3",
                        type="filepath",
                        interactive=False,
                        visible=True,
                    )
                    audio_flac = gr.Audio(
                        label="FLAC",
                        type="filepath",
                        interactive=False,
                        visible=True,
                    )

                with gr.Row():
                    with gr.Column(scale=3):
                        generate = gr.Button(
                            value="\U0001F3A7 " + "Сгенерировать",
                            variant="primary"
                        )

        def select_example_audio(audio_file):
            if audio_file:
                audio_path = os.path.join("examples",audio_file)
                base_name = os.path.splitext(audio_file)[0]
                
                # Попробуем найти текстовый файл в разных форматах
                text_content = ""
                for ext in ['.txt', '.lab']:
                    text_file = base_name + ext
                    text_path = os.path.join("examples", text_file)
                    if os.path.exists(text_path):
                        try:
                            with open(text_path, "r", encoding="utf-8") as f:
                                text_content = f.read().strip()
                                break
                        except:
                            continue
                
                return audio_path, text_content
            return None, ""

        example_audio_dropdown.change(
            fn=select_example_audio,
            inputs=[example_audio_dropdown],
            outputs=[reference_audio, reference_text]
        )

        generate.click(
            inference_wrapper,
            [
                text,
                reference_audio,
                reference_text,
                max_new_tokens,
                chunk_length,
                top_p,
                repetition_penalty,
                temperature,
                seed,
                wav_format,
                mp3_format,
                flac_format,
            ],
            [audio_wav, audio_mp3, audio_flac, error],
            concurrency_limit=1,
        )

    return app

def parse_args():
    parser = ArgumentParser()
    parser.add_argument(
        "--llama-checkpoint-path",
        type=Path,
        default="checkpoints/fish-speech-1.5",
    )
    parser.add_argument(
        "--decoder-checkpoint-path",
        type=Path,
        default="checkpoints/fish-speech-1.5/firefly-gan-vq-fsq-8x1024-21hz-generator.pth",
    )
    parser.add_argument("--decoder-config-name", type=str, default="firefly_gan_vq")
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--half", action="store_true")
    parser.add_argument("--compile", action="store_true", default=True)
    parser.add_argument("--max-gradio-length", type=int, default=0)
    parser.add_argument("--theme", type=str, default="light")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    args.precision = torch.half if args.half else torch.bfloat16

    logger.info("Loading Llama model...")
    llama_queue = launch_thread_safe_queue(
        checkpoint_path=args.llama_checkpoint_path,
        device=args.device,
        precision=args.precision,
        compile=args.compile,
    )
    logger.info("Llama model loaded, loading VQ-GAN model...")

    decoder_model = load_decoder_model(
        config_name=args.decoder_config_name,
        checkpoint_path=args.decoder_checkpoint_path,
        device=args.device,
    )

    logger.info("Decoder model loaded, warming up...")

    # Dry run to check if the model is loaded correctly and avoid the first-time latency
    list(
        inference(
            ServeTTSRequest(
                text="Hello world.",
                references=[],
                reference_id=None,
                max_new_tokens=0,
                chunk_length=200,
                top_p=0.7,
                repetition_penalty=1.5,
                temperature=0.7,
                emotion=None,
                format="wav",
                normalize=False,
                use_memory_cache="never"
            ),
            ['wav']  # Добавляем выбранные форматы для dry run
        )
    )

    logger.info("Warming up done, launching the web UI...")

    app = build_app()
    app.queue(api_open=True).launch(show_error=True, show_api=True, inbrowser=True)