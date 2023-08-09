import datetime
from fractions import Fraction
import importlib.util
import json
from math import ceil, pow, gcd
import os
import random
import re
import sys

import cv2
import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from skimage.feature import graycomatrix, graycoprops
import torch
import torch.nn.functional as F

import folder_paths
file_path = os.path.join(folder_paths.base_path, 'comfy_extras/nodes_clip_sdxl.py')
module_name = "nodes_clip_sdxl"
spec = importlib.util.spec_from_file_location(module_name, file_path)
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
from nodes_clip_sdxl import CLIPTextEncodeSDXL, CLIPTextEncodeSDXLRefiner
file_path = os.path.join(folder_paths.base_path, 'comfy_extras/nodes_upscale_model.py')
module_name = "nodes_upscale_model"
spec = importlib.util.spec_from_file_location(module_name, file_path)
module = importlib.util.module_from_spec(spec)
sys.modules[module_name] = module
spec.loader.exec_module(module)
from nodes_upscale_model import UpscaleModelLoader, ImageUpscaleWithModel
from comfy.model_management import unload_model, soft_empty_cache
from nodes import LoraLoader, ConditioningAverage, common_ksampler, ImageScale, VAEEncode, VAEDecode
import comfy.utils
from comfy_extras.chainner_models import model_loading
from comfy import model_management

def find_latent_size(width: int, height: int, res: int = 1024) -> (int, int):
    best_w = 0
    best_h = 0
    target_ratio = Fraction(width, height)

    for i in range(1, 256):
        for j in range(1, 256):
            if Fraction(8 * i, 8 * j) > target_ratio * 0.98 and Fraction(8 * i, 8 * j) < target_ratio and 8 * i * 8 * j <= res * res:
                candidates = [
                    (ceil(8 * i / 64) * 64, ceil(8 * j / 64) * 64),
                    (8 * i // 64 * 64, ceil(8 * j / 64) * 64),
                    (ceil(8 * i / 64) * 64, 8 * j // 64 * 64),
                    (8 * i // 64 * 64, 8 * j // 64 * 64),
                ]
                for w, h in candidates:
                    if w * h > res * res:
                        continue
                    if w * h > best_w * best_h:
                        best_w, best_h = w, h
    return best_w, best_h

def find_tile_dimensions(width: int, height: int, multiplier: float, res: int) -> (int, int):
    new_width = width * multiplier // 8 * 8
    new_height = height * multiplier // 8 * 8
    width_multiples = round(new_width / res, 0)
    height_multiples = round(new_height / res, 0)
    tile_width = new_width / width_multiples // 1
    tile_height = new_height / height_multiples // 1
    return tile_width, tile_height

def read_ratios():
    p = os.path.dirname(os.path.realpath(__file__))
    file_path = os.path.join(p, 'ratios.json')
    with open(file_path, 'r') as file:
        data = json.load(file)
    ratio_sizes = list(data['ratios'].keys())
    ratio_dict = data['ratios']
    # user_styles.json
    user_styles_path = os.path.join(folder_paths.base_path, 'user_ratios.json')
    # check if file exists
    if os.path.isfile(user_styles_path):
        # read json and update ratio_dict
        with open(user_styles_path, 'r') as file:
            user_data = json.load(file)
        for ratio in user_data['ratios']:
            ratio_dict[ratio] = user_data['ratios'][ratio]
            ratio_sizes.append(ratio)
    return ratio_sizes, ratio_dict

def read_styles():
    p = os.path.dirname(os.path.realpath(__file__))
    file_path = os.path.join(p, 'styles.json')
    with open(file_path, 'r') as file:
        data = json.load(file)
    # each style has a positive and negative key
    """ start of json styles.json looks like this:
    {
    "styles": {
        "none": {
        "positive": "",
        "negative": ""
        },
        "3d-model": {
        "positive": "3d model, polygons, mesh, textures, lighting, rendering",
        "negative": "2D representation, lack of depth and volume, no realistic rendering"
        },
    """
    styles = list(data['styles'].keys())
    pos_style = {}
    neg_style = {}
    for style in styles:
        pos_style[style] = data['styles'][style]['positive']
        neg_style[style] = data['styles'][style]['negative']
    # user_styles.json
    user_styles_path = os.path.join(folder_paths.base_path, 'user_styles.json')
    # check if file exists
    if os.path.isfile(user_styles_path):
        # read json and update pos_style and neg_style
        with open(user_styles_path, 'r') as file:
            user_data = json.load(file)
        for style in user_data['styles']:
            pos_style[style] = user_data['styles'][style]['positive']
            neg_style[style] = user_data['styles'][style]['negative']
            styles.append(style)
    return styles, pos_style, neg_style

def find_and_replace_wildcards(prompt, offset_seed, debug=False):
    # wildcards use the __file_name__ syntax with optional |word_to_find
    wildcard_path = os.path.join(folder_paths.base_path, 'wildcards')
    wildcard_regex = r'(\[(\d+)\$\$)?__((?:[^|_]+_)*[^|_]+)((?:\|[^|]+)*)__\]?'
    match_strings = []
    random.seed(offset_seed)
    offset = offset_seed
    for full_match, lines_count_str, actual_match, words_to_find_str in re.findall(wildcard_regex, prompt):
        words_to_find = words_to_find_str.split('|')[1:] if words_to_find_str else None
        if debug:
            print(f'Wildcard match: {actual_match}')
            print(f'Wildcard words to find: {words_to_find}')
        lines_to_insert = int(lines_count_str) if lines_count_str else 1
        if debug:
            print(f'Wildcard lines to insert: {lines_to_insert}')
        match_parts = actual_match.split('/')
        if len(match_parts) > 1:
            wildcard_dir = os.path.join(*match_parts[:-1])
            wildcard_file = match_parts[-1]
        else:
            wildcard_dir = ''
            wildcard_file = match_parts[0]
        search_path = os.path.join(wildcard_path, wildcard_dir)
        file_path = os.path.join(search_path, wildcard_file + '.txt')
        if not os.path.isfile(file_path) and wildcard_dir == '':
            file_path = os.path.join(wildcard_path, wildcard_file + '.txt')
        if os.path.isfile(file_path):
            if actual_match in match_strings:
                offset += 1
            selected_lines = []
            with open(file_path, 'r', encoding='utf-8') as file:
                file_lines = file.readlines()
                num_lines = len(file_lines)
                if words_to_find:
                    for i in range(lines_to_insert):
                        start_idx = (offset + i) % num_lines
                        for j in range(num_lines):
                            line_number = (start_idx + j) % num_lines
                            line = file_lines[line_number].strip()
                            if any(re.search(r'\b' + re.escape(word) + r'\b', line, re.IGNORECASE) for word in words_to_find):
                                selected_lines.append(line)
                                break
                else:
                    start_idx = offset % num_lines
                    for i in range(lines_to_insert):
                        line_number = (start_idx + i) % num_lines
                        line = file_lines[line_number].strip()
                        selected_lines.append(line)
            if len(selected_lines) == 1:
                replacement_text = selected_lines[0]
            else:
                replacement_text = ','.join(selected_lines)
            prompt = prompt.replace(full_match, replacement_text, 1)
            match_strings.append(actual_match)
            offset += lines_to_insert
            if debug:
                print('Wildcard prompt selected: ' + replacement_text)
        else:
            if debug:
                print(f'Wildcard file {wildcard_file}.txt not found in {search_path}')
    return prompt

def find_and_replace_wildcards(prompt, offset_seed, debug=False):
    # wildcards use the __file_name__ syntax with optional |word_to_find
    wildcard_path = os.path.join(folder_paths.base_path, 'wildcards')
    wildcard_regex = r'(\[(\d+)\$\$)?__((?:[^|_]+_)*[^|_]+)((?:\|[^|]+)*)__\]?'
    match_strings = []
    random.seed(offset_seed)
    offset = offset_seed

    new_prompt = ''
    last_end = 0

    for m in re.finditer(wildcard_regex, prompt):
        full_match, lines_count_str, actual_match, words_to_find_str = m.groups()
        # Append everything up to this match
        new_prompt += prompt[last_end:m.start()]

    #for full_match, lines_count_str, actual_match, words_to_find_str in re.findall(wildcard_regex, prompt):
        words_to_find = words_to_find_str.split('|')[1:] if words_to_find_str else None
        if debug:
            print(f'Wildcard match: {actual_match}')
            print(f'Wildcard words to find: {words_to_find}')
        lines_to_insert = int(lines_count_str) if lines_count_str else 1
        if debug:
            print(f'Wildcard lines to insert: {lines_to_insert}')
        match_parts = actual_match.split('/')
        if len(match_parts) > 1:
            wildcard_dir = os.path.join(*match_parts[:-1])
            wildcard_file = match_parts[-1]
        else:
            wildcard_dir = ''
            wildcard_file = match_parts[0]
        search_path = os.path.join(wildcard_path, wildcard_dir)
        file_path = os.path.join(search_path, wildcard_file + '.txt')
        if not os.path.isfile(file_path) and wildcard_dir == '':
            file_path = os.path.join(wildcard_path, wildcard_file + '.txt')
        if os.path.isfile(file_path):
            if actual_match in match_strings:
                offset += 1
            selected_lines = []
            with open(file_path, 'r', encoding='utf-8') as file:
                file_lines = file.readlines()
                num_lines = len(file_lines)
                if words_to_find:
                    for i in range(lines_to_insert):
                        start_idx = (offset + i) % num_lines
                        for j in range(num_lines):
                            line_number = (start_idx + j) % num_lines
                            line = file_lines[line_number].strip()
                            if any(re.search(r'\b' + re.escape(word) + r'\b', line, re.IGNORECASE) for word in words_to_find):
                                selected_lines.append(line)
                                break
                else:
                    start_idx = offset % num_lines
                    for i in range(lines_to_insert):
                        line_number = (start_idx + i) % num_lines
                        line = file_lines[line_number].strip()
                        selected_lines.append(line)
            if len(selected_lines) == 1:
                replacement_text = selected_lines[0]
            else:
                replacement_text = ','.join(selected_lines)
            new_prompt += replacement_text
            match_strings.append(actual_match)
            offset += lines_to_insert
            if debug:
                print('Wildcard prompt selected: ' + replacement_text)
        else:
            if debug:
                print(f'Wildcard file {wildcard_file}.txt not found in {search_path}')
        last_end = m.end()
    new_prompt += prompt[last_end:]
    return new_prompt


def strip_all_syntax(text):
    # replace any <lora:lora_name> with nothing
    text = re.sub(r'<lora:(.*?)>', '', text)
    # replace any <lora:lora_name:multiplier> with nothing
    text = re.sub(r'<lora:(.*?):(.*?)>', '', text)
    # replace any <style:style_name> with nothing
    text = re.sub(r'<style:(.*?)>', '', text)
    # replace any __wildcard_name__ with nothing
    text = re.sub(r'__(.*?)__', '', text)
    # replace any __wildcard_name|word__ with nothing
    text = re.sub(r'__(.*?)\|(.*?)__', '', text)
    # replace any [2$__wildcard__] with nothing
    text = re.sub(r'\[\d+\$(.*?)\]', '', text)
    # replace any [2$__wildcard|word__] with nothing
    text = re.sub(r'\[\d+\$(.*?)\|(.*?)\]', '', text)
    # replace double spaces with single spaces
    text = text.replace('  ', ' ')
    # replace double commas with single commas
    text = text.replace(',,', ',')
    # replace ` , ` with `, `
    text = text.replace(' , ', ', ')
    # replace leading and trailing spaces and commas
    text = text.strip(' ,')
    # clean up any < > [ ] or _ that are left over
    text = text.replace('<', '').replace('>', '').replace('[', '').replace(']', '').replace('_', '')
    return text

def add_metadata_to_dict(info_dict, **kwargs):
    for key, value in kwargs.items():
        if isinstance(value, (int, float, str)):
            if key not in info_dict:
                info_dict[key] = [value]
            else:
                info_dict[key].append(value)

def extract_and_load_loras(text, model, clip):
    # load loras detected in the prompt text
    # The text for adding LoRA to the prompt, <lora:filename:multiplier>, is only used to enable LoRA, and is erased from prompt afterwards
    # The multiplier is optional, and defaults to 1.0
    # We update the model and clip, and return the new model and clip with the lora prompt stripped from the text
    # If multiple lora prompts are detected we chain them together like: original clip > clip_with_lora1 > clip_with_lora2 > clip_with_lora3 > etc
    lora_re = r'<lora:(.*?)(?::(.*?))?>'
    # find all lora prompts
    lora_prompts = re.findall(lora_re, text)
    stripped_text = text
    # if we found any lora prompts
    if len(lora_prompts) > 0:
        # loop through each lora prompt
        for lora_prompt in lora_prompts:
            # get the lora filename
            lora_filename = lora_prompt[0]
            # check for file extension in filename
            if '.safetensors' not in lora_filename:
                lora_filename += '.safetensors'
            # get the lora multiplier
            lora_multiplier = float(lora_prompt[1]) if lora_prompt[1] != '' else 1.0
            print('Loading LoRA: ' + lora_filename + ' with multiplier: ' + str(lora_multiplier))
            # apply the lora to the clip using the LoraLoader.load_lora function
            # def load_lora(self, model, clip, lora_name, strength_model, strength_clip):
            # ...
            # return (model_lora, clip_lora)
            # apply the lora to the clip
            model, clip_lora = LoraLoader.load_lora(model, clip, lora_filename, lora_multiplier, lora_multiplier)
            stripped_text = stripped_text.replace(f'<lora:{lora_filename}:{lora_multiplier}>', '')
    return model, clip, stripped_text

def read_cluts():
    p = os.path.dirname(os.path.realpath(__file__))
    halddir = os.path.join(p, 'HaldCLUT')
    files = [os.path.join(halddir, f) for f in os.listdir(halddir) if os.path.isfile(os.path.join(halddir, f)) and f.endswith('.png')]
    return files

def apply_hald_clut(hald_img, img):
    hald_w, hald_h = hald_img.size
    clut_size = int(round(pow(hald_w, 1/3)))
    scale = (clut_size * clut_size - 1) / 255
    img = np.asarray(img)

    # Convert the HaldCLUT image to numpy array
    hald_img_array = np.asarray(hald_img)

    # If the HaldCLUT image is monochrome, duplicate its single channel to three
    if len(hald_img_array.shape) == 2:
        hald_img_array = np.stack([hald_img_array]*3, axis=-1)

    hald_img_array = hald_img_array.reshape(clut_size ** 6, 3)

    clut_r = np.rint(img[:, :, 0] * scale).astype(int)
    clut_g = np.rint(img[:, :, 1] * scale).astype(int)
    clut_b = np.rint(img[:, :, 2] * scale).astype(int)
    filtered_image = np.zeros((img.shape))
    filtered_image[:, :] = hald_img_array[clut_r + clut_size ** 2 * clut_g + clut_size ** 4 * clut_b]
    filtered_image = Image.fromarray(filtered_image.astype('uint8'), 'RGB')
    return filtered_image

def gamma_correction_pil(image, gamma):
    # Convert PIL Image to NumPy array
    img_array = np.array(image)
    # Normalization [0,255] -> [0,1]
    img_array = img_array / 255.0
    # Apply gamma correction
    img_corrected = np.power(img_array, gamma)
    # Convert corrected image back to original scale [0,1] -> [0,255]
    img_corrected = np.uint8(img_corrected * 255)
    # Convert NumPy array back to PIL Image
    corrected_image = Image.fromarray(img_corrected)
    return corrected_image

# Tensor to PIL
def tensor2pil(image):
    return Image.fromarray(np.clip(255. * image.cpu().numpy().squeeze(), 0, 255).astype(np.uint8))

# PIL to Tensor
def pil2tensor(image):
    return torch.from_numpy(np.array(image).astype(np.float32) / 255.0).unsqueeze(0)

def tensor2numpy(image):
    # Convert tensor to numpy array and transpose dimensions from (C, H, W) to (H, W, C)
    return (255.0 * image.cpu().numpy().squeeze().transpose(1, 2, 0)).astype(np.uint8)

class WildcardProcessor:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"prompt": ("STRING", {"multiline": True, "placeholder": "Prompt Text"}),
                             "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff})}}

    RETURN_TYPES = ('STRING',)
    FUNCTION = 'process'
    CATEGORY = 'Mikey/Text'

    def process(self, prompt, seed):
        prompt = find_and_replace_wildcards(prompt, seed)
        return (prompt, )

class HaldCLUT:
    @classmethod
    def INPUT_TYPES(s):
        s.haldclut_files = read_cluts()
        s.file_names = [os.path.basename(f) for f in s.haldclut_files]
        return {"required": {"image": ("IMAGE",),
                             "hald_clut": (s.file_names,),
                             "gamma_correction": (['True','False'],)}}

    RETURN_TYPES = ('IMAGE',)
    RETURN_NAMES = ('image,')
    FUNCTION = 'apply_haldclut'
    CATEGORY = 'Mikey/Image'
    OUTPUT_NODE = True

    def apply_haldclut(self, image, hald_clut, gamma_correction):
        hald_img = Image.open(self.haldclut_files[self.file_names.index(hald_clut)])
        img = tensor2pil(image)
        if gamma_correction == 'True':
            corrected_img = gamma_correction_pil(img, 1.0/2.2)
        else:
            corrected_img = img
        filtered_image = apply_hald_clut(hald_img, corrected_img).convert("RGB")
        return (pil2tensor(filtered_image), )

    @classmethod
    def IS_CHANGED(self, hald_clut):
        return (np.nan,)

class EmptyLatentRatioSelector:
    @classmethod
    def INPUT_TYPES(s):
        s.ratio_sizes, s.ratio_dict = read_ratios()
        return {'required': {'ratio_selected': (s.ratio_sizes,),
                             "batch_size": ("INT", {"default": 1, "min": 1, "max": 64})}}

    RETURN_TYPES = ('LATENT',)
    FUNCTION = 'generate'
    CATEGORY = 'Mikey/Latent'

    def generate(self, ratio_selected, batch_size=1):
        width = self.ratio_dict[ratio_selected]["width"]
        height = self.ratio_dict[ratio_selected]["height"]
        latent = torch.zeros([batch_size, 4, height // 8, width // 8])
        return ({"samples":latent}, )

class EmptyLatentRatioCustom:
    @classmethod
    def INPUT_TYPES(s):
        s.ratio_sizes, s.ratio_dict = read_ratios()
        return {"required": { "width": ("INT", {"default": 1024, "min": 1, "max": 8192, "step": 1}),
                              "height": ("INT", {"default": 1024, "min": 1, "max": 8192, "step": 1}),
                              "batch_size": ("INT", {"default": 1, "min": 1, "max": 64})}}

    RETURN_TYPES = ('LATENT',)
    FUNCTION = 'generate'
    CATEGORY = 'Mikey/Latent'

    def generate(self, width, height, batch_size=1):
        # solver
        if width == 1 and height == 1 or width == height:
            w, h = 1024, 1024
        if f'{width}:{height}' in self.ratio_dict:
            w, h = self.ratio_dict[f'{width}:{height}']
        else:
            w, h = find_latent_size(width, height)
        latent = torch.zeros([batch_size, 4, h // 8, w // 8])
        return ({"samples":latent}, )

class ResizeImageSDXL:
    crop_methods = ["disabled", "center"]
    upscale_methods = ["nearest-exact", "bilinear", "area", "bicubic"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "image": ("IMAGE",), "upscale_method": (s.upscale_methods,),
                              "crop": (s.crop_methods,)},
                "optional": { "mask": ("MASK", )}}

    RETURN_TYPES = ('IMAGE',)
    FUNCTION = 'resize'
    CATEGORY = 'Mikey/Image'

    def upscale(self, image, upscale_method, width, height, crop):
        samples = image.movedim(-1,1)
        s = comfy.utils.common_upscale(samples, width, height, upscale_method, crop)
        s = s.movedim(1,-1)
        return (s,)

    def resize(self, image, upscale_method, crop):
        w, h = find_latent_size(image.shape[2], image.shape[1])
        print('Resizing image from {}x{} to {}x{}'.format(image.shape[2], image.shape[1], w, h))
        img = self.upscale(image, upscale_method, w, h, crop)[0]
        return (img, )

class BatchResizeImageSDXL(ResizeImageSDXL):
    crop_methods = ["disabled", "center"]
    upscale_methods = ["nearest-exact", "bilinear", "area", "bicubic"]

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"image_directory": ("STRING", {"multiline": False, "placeholder": "Image Directory"}),
                             "upscale_method": (s.upscale_methods,),
                             "crop": (s.crop_methods,)},}

    RETURN_TYPES = ('IMAGE',)
    RETURN_NAMES = ('image',)
    FUNCTION = 'batch'
    CATEGORY = 'Mikey/Image'
    OUTPUT_IS_LIST = (True, )

    def batch(self, image_directory, upscale_method, crop):
        if not os.path.exists(image_directory):
            raise Exception(f"Image directory {image_directory} does not exist")

        images = []
        for file in os.listdir(image_directory):
            if file.endswith('.png') or file.endswith('.jpg') or file.endswith('.jpeg') or file.endswith('.webp') or file.endswith('.bmp') or file.endswith('.gif'):
                img = Image.open(os.path.join(image_directory, file))
                img = pil2tensor(img)
                # resize image
                img = self.resize(img, upscale_method, crop)[0]
                images.append(img)
        return (images,)

def get_save_image_path(filename_prefix, output_dir, image_width=0, image_height=0):
    def map_filename(filename):
        try:
            # Ignore files that are not images
            if not filename.endswith('.png'):
                return 0
            # Assuming filenames are in the format you provided,
            # the counter would be the second last item when splitting by '_'
            digits = int(filename.split('_')[-2])
        except:
            digits = 0
        return digits

    def compute_vars(input, image_width, image_height):
        input = input.replace("%width%", str(image_width))
        input = input.replace("%height%", str(image_height))
        return input

    filename_prefix = compute_vars(filename_prefix, image_width, image_height)

    subfolder = os.path.dirname(os.path.normpath(filename_prefix))
    filename = os.path.basename(os.path.normpath(filename_prefix))

    # Remove trailing period from filename, if present
    if filename.endswith('.'):
        filename = filename[:-1]

    full_output_folder = os.path.join(output_dir, subfolder)

    if os.path.commonpath((output_dir, os.path.abspath(full_output_folder))) != output_dir:
        print("Saving image outside the output folder is not allowed.")
        return {}

    try:
        counter = max(map(map_filename, os.listdir(full_output_folder)), default=0) + 1
    except FileNotFoundError:
        os.makedirs(full_output_folder, exist_ok=True)
        counter = 1
    return full_output_folder, filename, counter, subfolder, filename_prefix

class SaveImagesMikey:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()
        self.type = "output"

    @classmethod
    def INPUT_TYPES(s):
        return {"required":
                    {"images": ("IMAGE", ),
                     "positive_prompt": ("STRING", {'default': 'Positive Prompt'}),
                     "negative_prompt": ("STRING", {'default': 'Negative Prompt'}),},
                     "filename_prefix": ("STRING", {"default": ""}),
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},
                }

    RETURN_TYPES = ()
    FUNCTION = "save_images"
    OUTPUT_NODE = True
    CATEGORY = "Mikey/Image"

    def save_images(self, images, filename_prefix='', prompt=None, extra_pnginfo=None, positive_prompt='', negative_prompt=''):
        full_output_folder, filename, counter, subfolder, filename_prefix = get_save_image_path(filename_prefix, self.output_dir, images[0].shape[1], images[0].shape[0])
        results = list()
        for image in images:
            i = 255. * image.cpu().numpy()
            img = Image.fromarray(np.clip(i, 0, 255).astype(np.uint8))
            metadata = PngInfo()
            pos_trunc = ''
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt, ensure_ascii=False))
            if extra_pnginfo is not None:
                for x in extra_pnginfo:
                    metadata.add_text(x, json.dumps(extra_pnginfo[x], ensure_ascii=False))
            if positive_prompt:
                metadata.add_text("positive_prompt", json.dumps(positive_prompt, ensure_ascii=False))
                # replace any special characters with nothing and spaces with _
                clean_pos = re.sub(r'[^a-zA-Z0-9 ]', '', positive_prompt)
                pos_trunc = clean_pos.replace(' ', '_')[0:80]
            if negative_prompt:
                metadata.add_text("negative_prompt", json.dumps(negative_prompt, ensure_ascii=False))
            ts_str = datetime.datetime.now().strftime("%y%m%d%H%M%S")
            file = f"{ts_str}_{pos_trunc}_{filename}_{counter:05}_.png"
            img.save(os.path.join(full_output_folder, file), pnginfo=metadata, compress_level=4)
            results.append({
                "filename": file,
                "subfolder": subfolder,
                "type": self.type
            })
            counter += 1

        return { "ui": { "images": results } }

class AddMetaData:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {"image": ("IMAGE",),
                             "label": ("STRING", {"multiline": False, "placeholder": "Label for metadata"}),
                             "text_value": ("STRING", {"multiline": True, "placeholder": "Text to add to metadata"})},
                "hidden": {"extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ('IMAGE',)
    FUNCTION = "add_metadata"
    CATEGORY = "Mikey/Meta"
    OUTPUT_NODE = True

    def add_metadata(self, image, label, text_value, prompt=None, extra_pnginfo=None):
        if extra_pnginfo is None:
            extra_pnginfo = {}
        if label in extra_pnginfo:
            extra_pnginfo[label] += ', ' + text_value
        else:
            extra_pnginfo[label] = text_value
        return (image,)

class SaveMetaData:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {'image': ('IMAGE',),
                             'filename_prefix': ("STRING", {"default": ""}),
                             'timestamp_prefix': (['true','false'], {'default':'true'}),
                             'counter': (['true','false'], {'default':'true'}),},
                "hidden": {"prompt": "PROMPT", "extra_pnginfo": "EXTRA_PNGINFO"},}

    RETURN_TYPES = ()
    FUNCTION = "save_metadata"
    CATEGORY = "Mikey/Meta"
    OUTPUT_NODE = True

    def save_metadata(self, image, filename_prefix, timestamp_prefix, counter, prompt=None, extra_pnginfo=None):
        # save metatdata to txt file
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, folder_paths.get_output_directory(), 1, 1)
        ts_str = datetime.datetime.now().strftime("%y%m%d%H%M")
        filen = ''
        if timestamp_prefix == 'true':
            filen += ts_str + '_'
        filen = filen + filename_prefix
        if counter == 'true':
            filen += '_' + str(counter)
        filename = filen + '.txt'
        file_path = os.path.join(full_output_folder, filename)
        with open(file_path, 'w') as file:
            for key, value in extra_pnginfo.items():
                file.write(f'{key}: {value}\n')
            for key, value in prompt.items():
                file.write(f'{key}: {value}\n')
        return {'save_metadata': {'filename': filename, 'subfolder': subfolder}}

class FileNamePrefix:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {'date': (['true','false'], {'default':'true'}),
                             'date_directory': (['true','false'], {'default':'true'}),
                             'custom_text': ('STRING', {'default': ''})}}

    RETURN_TYPES = ('STRING',)
    RETURN_NAMES = ('filename_prefix',)
    FUNCTION = 'get_filename_prefix'
    CATEGORY = 'Mikey/Meta'

    def get_filename_prefix(self, date, date_directory, custom_directory, custom_text):
        filename_prefix = ''
        if date_directory == 'true':
            ts_str = datetime.datetime.now().strftime("%y%m%d")
            filename_prefix += ts_str + '/'
        if date == 'true':
            ts_str = datetime.datetime.now().strftime("%y%m%d%H%M")
            filename_prefix += ts_str
        if custom_text != '':
            filename_prefix += '_' + custom_text
        return (filename_prefix,)

class PromptWithStyle:
    @classmethod
    def INPUT_TYPES(s):
        s.ratio_sizes, s.ratio_dict = read_ratios()
        s.styles, s.pos_style, s.neg_style = read_styles()
        return {"required": {"positive_prompt": ("STRING", {"multiline": True, 'default': 'Positive Prompt'}),
                             "negative_prompt": ("STRING", {"multiline": True, 'default': 'Negative Prompt'}),
                             "style": (s.styles,),
                             "ratio_selected": (s.ratio_sizes,),
                             "batch_size": ("INT", {"default": 1, "min": 1, "max": 64}),
                             "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                             }
        }

    RETURN_TYPES = ('LATENT','STRING','STRING','STRING','STRING','INT','INT','INT','INT',)
    RETURN_NAMES = ('samples','positive_prompt_text_g','negative_prompt_text_g','positive_style_text_l',
                    'negative_style_text_l','width','height','refiner_width','refiner_height',)
    FUNCTION = 'start'
    CATEGORY = 'Mikey'
    OUTPUT_NODE = True

    def start(self, positive_prompt, negative_prompt, style, ratio_selected, batch_size, seed):
        # first process wildcards
        print('Positive Prompt Entered:', positive_prompt)
        pos_prompt = find_and_replace_wildcards(positive_prompt, seed, debug=True)
        print('Positive Prompt:', pos_prompt)
        print('Negative Prompt Entered:', negative_prompt)
        neg_prompt = find_and_replace_wildcards(negative_prompt, seed, debug=True)
        print('Negative Prompt:', neg_prompt)
        if pos_prompt != '' and pos_prompt != 'Positive Prompt' and pos_prompt is not None:
            if '{prompt}' in self.pos_style[style]:
                pos_prompt = self.pos_style[style].replace('{prompt}', pos_prompt)
            else:
                if self.pos_style[style]:
                    pos_prompt = pos_prompt + ', ' + self.pos_style[style]
        else:
            pos_prompt = self.pos_style[style]
        if neg_prompt != '' and neg_prompt != 'Negative Prompt' and neg_prompt is not None:
            if '{prompt}' in self.neg_style[style]:
                neg_prompt = self.neg_style[style].replace('{prompt}', neg_prompt)
            else:
                if self.neg_style[style]:
                    neg_prompt = neg_prompt + ', ' + self.neg_style[style]
        else:
            neg_prompt = self.neg_style[style]
        width = self.ratio_dict[ratio_selected]["width"]
        height = self.ratio_dict[ratio_selected]["height"]
        # calculate dimensions for target_width, target height (base) and refiner_width, refiner_height (refiner)
        ratio = min([width, height]) / max([width, height])
        target_width, target_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        refiner_width = target_width
        refiner_height = target_height
        print('Width:', width, 'Height:', height,
              'Target Width:', target_width, 'Target Height:', target_height,
              'Refiner Width:', refiner_width, 'Refiner Height:', refiner_height)
        latent = torch.zeros([batch_size, 4, height // 8, width // 8])
        return ({"samples":latent},
                str(pos_prompt),
                str(neg_prompt),
                str(self.pos_style[style]),
                str(self.neg_style[style]),
                width,
                height,
                refiner_width,
                refiner_height,)

class PromptWithStyleV2:
    @classmethod
    def INPUT_TYPES(s):
        s.ratio_sizes, s.ratio_dict = read_ratios()
        s.styles, s.pos_style, s.neg_style = read_styles()
        return {"required": {"positive_prompt": ("STRING", {"multiline": True, 'default': 'Positive Prompt'}),
                             "negative_prompt": ("STRING", {"multiline": True, 'default': 'Negative Prompt'}),
                             "style": (s.styles,),
                             "ratio_selected": (s.ratio_sizes,),
                             "batch_size": ("INT", {"default": 1, "min": 1, "max": 64}),
                             "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                             "clip_base": ("CLIP",), "clip_refiner": ("CLIP",),
                             }
        }

    RETURN_TYPES = ('LATENT',
                    'CONDITIONING','CONDITIONING','CONDITIONING','CONDITIONING',
                    'STRING','STRING')
    RETURN_NAMES = ('samples',
                    'base_pos_cond','base_neg_cond','refiner_pos_cond','refiner_neg_cond',
                    'positive_prompt','negative_prompt')

    FUNCTION = 'start'
    CATEGORY = 'Mikey'

    def start(self, clip_base, clip_refiner, positive_prompt, negative_prompt, style, ratio_selected, batch_size, seed):
        """ get output from PromptWithStyle.start """
        (latent,
         pos_prompt, neg_prompt,
         pos_style, neg_style,
         width, height,
         refiner_width, refiner_height) = PromptWithStyle.start(self, positive_prompt,
                                                                negative_prompt,
                                                                style, ratio_selected,
                                                                batch_size, seed)
        # calculate dimensions for target_width, target height (base) and refiner_width, refiner_height (refiner)
        ratio = min([width, height]) / max([width, height])
        target_width, target_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        refiner_width = target_width
        refiner_height = target_height
        print('Width:', width, 'Height:', height,
              'Target Width:', target_width, 'Target Height:', target_height,
              'Refiner Width:', refiner_width, 'Refiner Height:', refiner_height)
        # encode text
        sdxl_pos_cond = CLIPTextEncodeSDXL.encode(self, clip_base, width, height, 0, 0, target_width, target_height, pos_prompt, pos_style)[0]
        sdxl_neg_cond = CLIPTextEncodeSDXL.encode(self, clip_base, width, height, 0, 0, target_width, target_height, neg_prompt, neg_style)[0]
        refiner_pos_cond = CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 6, refiner_width, refiner_height, pos_prompt)[0]
        refiner_neg_cond = CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 2.5, refiner_width, refiner_height, neg_prompt)[0]
        # return
        return (latent,
                sdxl_pos_cond, sdxl_neg_cond,
                refiner_pos_cond, refiner_neg_cond,
                pos_prompt, neg_prompt)

class PromptWithSDXL:
    @classmethod
    def INPUT_TYPES(s):
        s.ratio_sizes, s.ratio_dict = read_ratios()
        return {"required": {"positive_prompt": ("STRING", {"multiline": True, 'default': 'Positive Prompt'}),
                             "negative_prompt": ("STRING", {"multiline": True, 'default': 'Negative Prompt'}),
                             "positive_style": ("STRING", {"multiline": True, 'default': 'Positive Style'}),
                             "negative_style": ("STRING", {"multiline": True, 'default': 'Negative Style'}),
                             "ratio_selected": (s.ratio_sizes,),
                             "batch_size": ("INT", {"default": 1, "min": 1, "max": 64}),
                             "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff})
                             }
        }

    RETURN_TYPES = ('LATENT','STRING','STRING','STRING','STRING','INT','INT','INT','INT',)
    RETURN_NAMES = ('samples','positive_prompt_text_g','negative_prompt_text_g','positive_style_text_l',
                    'negative_style_text_l','width','height','refiner_width','refiner_height',)
    FUNCTION = 'start'
    CATEGORY = 'Mikey'
    OUTPUT_NODE = True

    def start(self, positive_prompt, negative_prompt, positive_style, negative_style, ratio_selected, batch_size, seed):
        positive_prompt = find_and_replace_wildcards(positive_prompt, seed)
        negative_prompt = find_and_replace_wildcards(negative_prompt, seed)
        width = self.ratio_dict[ratio_selected]["width"]
        height = self.ratio_dict[ratio_selected]["height"]
        latent = torch.zeros([batch_size, 4, height // 8, width // 8])
        # calculate dimensions for target_width, target height (base) and refiner_width, refiner_height (refiner)
        ratio = min([width, height]) / max([width, height])
        target_width, target_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        refiner_width = target_width
        refiner_height = target_height
        print('Width:', width, 'Height:', height,
              'Target Width:', target_width, 'Target Height:', target_height,
              'Refiner Width:', refiner_width, 'Refiner Height:', refiner_height)
        return ({"samples":latent},
                str(positive_prompt),
                str(negative_prompt),
                str(positive_style),
                str(negative_style),
                width,
                height,
                refiner_width,
                refiner_height,)

class PromptWithStyleV3:
    def __init__(self):
        self.loaded_lora = None

    @classmethod
    def INPUT_TYPES(s):
        s.ratio_sizes, s.ratio_dict = read_ratios()
        s.styles, s.pos_style, s.neg_style = read_styles()
        s.fit = ['true','false']
        s.custom_size = ['true', 'false']
        return {"required": {"positive_prompt": ("STRING", {"multiline": True, 'default': 'Positive Prompt'}),
                             "negative_prompt": ("STRING", {"multiline": True, 'default': 'Negative Prompt'}),
                             "ratio_selected": (s.ratio_sizes,),
                             "custom_size": (s.custom_size, {"default": "false"}),
                             "fit_custom_size": (s.fit,),
                             "custom_width": ("INT", {"default": 1024, "min": 1, "max": 8192, "step": 1}),
                             "custom_height": ("INT", {"default": 1024, "min": 1, "max": 8192, "step": 1}),
                             "batch_size": ("INT", {"default": 1, "min": 1, "max": 64}),
                             "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                             "target_mode": (["match", "2x", "4x", "2x90", "4x90",
                                              "2048","2048-90","4096", "4096-90"], {"default": "4x"}),
                             "base_model": ("MODEL",), "clip_base": ("CLIP",), "clip_refiner": ("CLIP",),
                             },
                "hidden": {"extra_pnginfo": "EXTRA_PNGINFO"},
        }

    RETURN_TYPES = ('MODEL','LATENT',
                    'CONDITIONING','CONDITIONING','CONDITIONING','CONDITIONING',
                    'STRING','STRING')
    RETURN_NAMES = ('base_model','samples',
                    'base_pos_cond','base_neg_cond','refiner_pos_cond','refiner_neg_cond',
                    'positive_prompt','negative_prompt')

    FUNCTION = 'start'
    CATEGORY = 'Mikey'

    def extract_and_load_loras(self, text, model, clip):
        # load loras detected in the prompt text
        # The text for adding LoRA to the prompt, <lora:filename:multiplier>, is only used to enable LoRA, and is erased from prompt afterwards
        # The multiplier is optional, and defaults to 1.0
        # We update the model and clip, and return the new model and clip with the lora prompt stripped from the text
        # If multiple lora prompts are detected we chain them together like: original clip > clip_with_lora1 > clip_with_lora2 > clip_with_lora3 > etc
        lora_re = r'<lora:(.*?)(?::(.*?))?>'
        # find all lora prompts
        lora_prompts = re.findall(lora_re, text)
        stripped_text = text
        # if we found any lora prompts
        if len(lora_prompts) > 0:
            # loop through each lora prompt
            for lora_prompt in lora_prompts:
                # get the lora filename
                lora_filename = lora_prompt[0]
                # check for file extension in filename
                if '.safetensors' not in lora_filename:
                    lora_filename += '.safetensors'
                # get the lora multiplier
                lora_multiplier = float(lora_prompt[1]) if lora_prompt[1] != '' else 1.0
                print('Loading LoRA: ' + lora_filename + ' with multiplier: ' + str(lora_multiplier))
                # apply the lora to the clip using the LoraLoader.load_lora function
                # def load_lora(self, model, clip, lora_name, strength_model, strength_clip):
                # ...
                # return (model_lora, clip_lora)
                # apply the lora to the clip
                model, clip_lora = LoraLoader.load_lora(self, model, clip, lora_filename, lora_multiplier, lora_multiplier)
                stripped_text = stripped_text.replace(f'<lora:{lora_filename}:{lora_multiplier}>', '')
                stripped_text = stripped_text.replace(f'<lora:{lora_filename}>', '')
        return model, clip, stripped_text

    def parse_prompts(self, positive_prompt, negative_prompt, style, seed):
        positive_prompt = find_and_replace_wildcards(positive_prompt, seed, debug=True)
        negative_prompt = find_and_replace_wildcards(negative_prompt, seed, debug=True)
        if '{prompt}' in self.pos_style[style]:
            positive_prompt = self.pos_style[style].replace('{prompt}', positive_prompt)
        if positive_prompt == '' or positive_prompt == 'Positive Prompt' or positive_prompt is None:
            pos_prompt = self.pos_style[style]
        else:
            pos_prompt = positive_prompt + ', ' + self.pos_style[style]
        if negative_prompt == '' or negative_prompt == 'Negative Prompt' or negative_prompt is None:
            neg_prompt = self.neg_style[style]
        else:
            neg_prompt = negative_prompt + ', ' + self.neg_style[style]
        return pos_prompt, neg_prompt

    def start(self, base_model, clip_base, clip_refiner, positive_prompt, negative_prompt, ratio_selected, batch_size, seed,
              custom_size='false', fit_custom_size='false', custom_width=1024, custom_height=1024, target_mode='match',
              extra_pnginfo=None):
        if extra_pnginfo is None:
            extra_pnginfo = {'PromptWithStyle': {}}

        prompt_with_style = extra_pnginfo.get('PromptWithStyle', {})

        add_metadata_to_dict(prompt_with_style, positive_prompt=positive_prompt, negative_prompt=negative_prompt,
                            ratio_selected=ratio_selected, batch_size=batch_size, seed=seed, custom_size=custom_size,
                            fit_custom_size=fit_custom_size, custom_width=custom_width, custom_height=custom_height,
                            target_mode=target_mode)

        if custom_size == 'true':
            if fit_custom_size == 'true':
                if custom_width == 1 and custom_height == 1:
                    width, height = 1024, 1024
                if custom_width == custom_height:
                    width, height = 1024, 1024
                if f'{custom_width}:{custom_height}' in self.ratio_dict:
                    width, height = self.ratio_dict[f'{custom_width}:{custom_height}']
                else:
                    width, height = find_latent_size(custom_width, custom_height)
            else:
                width, height = custom_width, custom_height
        else:
            width = self.ratio_dict[ratio_selected]["width"]
            height = self.ratio_dict[ratio_selected]["height"]

        latent = torch.zeros([batch_size, 4, height // 8, width // 8])
        print(batch_size, 4, height // 8, width // 8)
        # calculate dimensions for target_width, target height (base) and refiner_width, refiner_height (refiner)
        ratio = min([width, height]) / max([width, height])
        if target_mode == 'match':
            target_width, target_height = width, height
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '2x':
            target_width, target_height = width * 2, height * 2
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '4x':
            target_width, target_height = width * 4, height * 4
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '2x90':
            target_width, target_height = height * 2, width * 2
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '4x90':
            target_width, target_height = height * 4, width * 4
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '4096':
            target_width, target_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '4096-90':
            target_width, target_height = (4096, 4096 * ratio // 8 * 8) if width < height else (4096 * ratio // 8 * 8, 4096)
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '2048':
            target_width, target_height = (2048, 2048 * ratio // 8 * 8) if width > height else (2048 * ratio // 8 * 8, 2048)
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        elif target_mode == '2048-90':
            target_width, target_height = (2048, 2048 * ratio // 8 * 8) if width < height else (2048 * ratio // 8 * 8, 2048)
            refiner_width, refiner_height = width * 4, height * 4
            #refiner_width, refiner_height = (4096, 4096 * ratio // 8 * 8) if width > height else (4096 * ratio // 8 * 8, 4096)
        print('Width:', width, 'Height:', height,
              'Target Width:', target_width, 'Target Height:', target_height,
              'Refiner Width:', refiner_width, 'Refiner Height:', refiner_height)
        add_metadata_to_dict(prompt_with_style, width=width, height=height, target_width=target_width, target_height=target_height,
                             refiner_width=refiner_width, refiner_height=refiner_height, crop_w=0, crop_h=0)
        # check for $style in prompt, split the prompt into prompt and style
        user_added_style = False
        if '$style' in positive_prompt:
            self.styles.append('user_added_style')
            self.pos_style['user_added_style'] = positive_prompt.split('$style')[1].strip()
            self.neg_style['user_added_style'] = ''
            user_added_style = True
        if '$style' in negative_prompt:
            if 'user_added_style' not in self.styles:
                self.styles.append('user_added_style')
            self.neg_style['user_added_style'] = negative_prompt.split('$style')[1].strip()
            user_added_style = True
        if user_added_style:
            positive_prompt = positive_prompt.split('$style')[0].strip()
            if '$style' in negative_prompt:
                negative_prompt = negative_prompt.split('$style')[0].strip()
            positive_prompt = positive_prompt + '<style:user_added_style>'

        # first process wildcards
        positive_prompt_ = find_and_replace_wildcards(positive_prompt, seed, True)
        negative_prompt_ = find_and_replace_wildcards(negative_prompt, seed, True)
        add_metadata_to_dict(prompt_with_style, positive_prompt=positive_prompt_, negative_prompt=negative_prompt_)
        if len(positive_prompt_) != len(positive_prompt) or len(negative_prompt_) != len(negative_prompt):
            seed += random.randint(0, 1000000)
        positive_prompt = positive_prompt_
        negative_prompt = negative_prompt_
        # extract and load loras
        base_model, clip_base_pos, pos_prompt = self.extract_and_load_loras(positive_prompt, base_model, clip_base)
        base_model, clip_base_neg, neg_prompt = self.extract_and_load_loras(negative_prompt, base_model, clip_base)
        # find and replace style syntax
        # <style:style_name> will update the selected style
        style_re = r'<style:(.*?)>'
        pos_style_prompts = re.findall(style_re, pos_prompt)
        neg_style_prompts = re.findall(style_re, neg_prompt)
        # concat style prompts
        style_prompts = pos_style_prompts + neg_style_prompts
        print(style_prompts)
        base_pos_conds = []
        base_neg_conds = []
        refiner_pos_conds = []
        refiner_neg_conds = []
        if len(style_prompts) == 0:
            style_ = 'none'
            pos_prompt_, neg_prompt_ = self.parse_prompts(positive_prompt, negative_prompt, style_, seed)
            pos_style_, neg_style_ = pos_prompt_, neg_prompt_
            pos_prompt_, neg_prompt_ = strip_all_syntax(pos_prompt_), strip_all_syntax(neg_prompt_)
            pos_style_, neg_style_ = strip_all_syntax(pos_style_), strip_all_syntax(neg_style_)
            print("pos_prompt_", pos_prompt_)
            print("neg_prompt_", neg_prompt_)
            print("pos_style_", pos_style_)
            print("neg_style_", neg_style_)
            # encode text
            add_metadata_to_dict(prompt_with_style, style=style_, clip_g_positive=pos_prompt, clip_l_positive=pos_style_)
            add_metadata_to_dict(prompt_with_style, clip_g_negative=neg_prompt, clip_l_negative=neg_style_)
            sdxl_pos_cond = CLIPTextEncodeSDXL.encode(self, clip_base_pos, width, height, 0, 0, target_width, target_height, pos_prompt_, pos_style_)[0]
            sdxl_neg_cond = CLIPTextEncodeSDXL.encode(self, clip_base_neg, width, height, 0, 0, target_width, target_height, neg_prompt_, neg_style_)[0]
            refiner_pos_cond = CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 6, refiner_width, refiner_height, pos_prompt_)[0]
            refiner_neg_cond = CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 2.5, refiner_width, refiner_height, neg_prompt_)[0]
            return (base_model, {"samples":latent},
                    sdxl_pos_cond, sdxl_neg_cond,
                    refiner_pos_cond, refiner_neg_cond,
                    pos_prompt_, neg_prompt_, {'extra_pnginfo': extra_pnginfo})

        for style_prompt in style_prompts:
            """ get output from PromptWithStyle.start """
            # strip all style syntax from prompt
            style_ = style_prompt
            print(style_ in self.styles)
            if style_ not in self.styles:
                # try to match a key without being case sensitive
                style_search = next((x for x in self.styles if x.lower() == style_.lower()), None)
                # if there are still no matches
                if style_search is None:
                    print(f'Could not find style: {style_}')
                    style_ = 'none'
                    continue
                else:
                    style_ = style_search
            pos_prompt_ = re.sub(style_re, '', pos_prompt)
            neg_prompt_ = re.sub(style_re, '', neg_prompt)
            pos_prompt_, neg_prompt_ = self.parse_prompts(pos_prompt_, neg_prompt_, style_, seed)
            pos_style_, neg_style_ = str(self.pos_style[style_]), str(self.neg_style[style_])
            pos_prompt_, neg_prompt_ = strip_all_syntax(pos_prompt_), strip_all_syntax(neg_prompt_)
            pos_style_, neg_style_ = strip_all_syntax(pos_style_), strip_all_syntax(neg_style_)
            add_metadata_to_dict(prompt_with_style, style=style_, positive_prompt=pos_prompt_, negative_prompt=neg_prompt_,
                                 positive_style=pos_style_, negative_style=neg_style_)
            #base_model, clip_base_pos, pos_prompt_ = self.extract_and_load_loras(pos_prompt_, base_model, clip_base)
            #base_model, clip_base_neg, neg_prompt_ = self.extract_and_load_loras(neg_prompt_, base_model, clip_base)
            width_, height_ = width, height
            refiner_width_, refiner_height_ = refiner_width, refiner_height
            # encode text
            add_metadata_to_dict(prompt_with_style, style=style_, clip_g_positive=pos_prompt_, clip_l_positive=pos_style_)
            add_metadata_to_dict(prompt_with_style, clip_g_negative=neg_prompt_, clip_l_negative=neg_style_)
            base_pos_conds.append(CLIPTextEncodeSDXL.encode(self, clip_base_pos, width_, height_, 0, 0, target_width, target_height, pos_prompt_, pos_style_)[0])
            base_neg_conds.append(CLIPTextEncodeSDXL.encode(self, clip_base_neg, width_, height_, 0, 0, target_width, target_height, neg_prompt_, neg_style_)[0])
            refiner_pos_conds.append(CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 6, refiner_width_, refiner_height_, pos_prompt_)[0])
            refiner_neg_conds.append(CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 2.5, refiner_width_, refiner_height_, neg_prompt_)[0])
        # if none of the styles matched we will get an empty list so we need to check for that again
        if len(base_pos_conds) == 0:
            style_ = 'none'
            pos_prompt_, neg_prompt_ = self.parse_prompts(positive_prompt, negative_prompt, style_, seed)
            pos_style_, neg_style_ = pos_prompt_, neg_prompt_
            pos_prompt_, neg_prompt_ = strip_all_syntax(pos_prompt_), strip_all_syntax(neg_prompt_)
            pos_style_, neg_style_ = strip_all_syntax(pos_style_), strip_all_syntax(neg_style_)
            # encode text
            add_metadata_to_dict(prompt_with_style, style=style_, clip_g_positive=pos_prompt_, clip_l_positive=pos_style_)
            add_metadata_to_dict(prompt_with_style, clip_g_negative=neg_prompt_, clip_l_negative=neg_style_)
            sdxl_pos_cond = CLIPTextEncodeSDXL.encode(self, clip_base_pos, width, height, 0, 0, target_width, target_height, pos_prompt_, pos_style_)[0]
            sdxl_neg_cond = CLIPTextEncodeSDXL.encode(self, clip_base_neg, width, height, 0, 0, target_width, target_height, neg_prompt_, neg_style_)[0]
            refiner_pos_cond = CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 6, refiner_width, refiner_height, pos_prompt_)[0]
            refiner_neg_cond = CLIPTextEncodeSDXLRefiner.encode(self, clip_refiner, 2.5, refiner_width, refiner_height, neg_prompt_)[0]
            return (base_model, {"samples":latent},
                    sdxl_pos_cond, sdxl_neg_cond,
                    refiner_pos_cond, refiner_neg_cond,
                    pos_prompt_, neg_prompt_, {'extra_pnginfo': extra_pnginfo})
        # loop through conds and add them together
        sdxl_pos_cond = base_pos_conds[0]
        weight = 1
        if len(base_pos_conds) > 1:
            for i in range(1, len(base_pos_conds)):
                weight += 1
                sdxl_pos_cond = ConditioningAverage.addWeighted(self, base_pos_conds[i], sdxl_pos_cond, 1 / weight)[0]
        sdxl_neg_cond = base_neg_conds[0]
        weight = 1
        if len(base_neg_conds) > 1:
            for i in range(1, len(base_neg_conds)):
                weight += 1
                sdxl_neg_cond = ConditioningAverage.addWeighted(self, base_neg_conds[i], sdxl_neg_cond, 1 / weight)[0]
        refiner_pos_cond = refiner_pos_conds[0]
        weight = 1
        if len(refiner_pos_conds) > 1:
            for i in range(1, len(refiner_pos_conds)):
                weight += 1
                refiner_pos_cond = ConditioningAverage.addWeighted(self, refiner_pos_conds[i], refiner_pos_cond, 1 / weight)[0]
        refiner_neg_cond = refiner_neg_conds[0]
        weight = 1
        if len(refiner_neg_conds) > 1:
            for i in range(1, len(refiner_neg_conds)):
                weight += 1
                refiner_neg_cond = ConditioningAverage.addWeighted(self, refiner_neg_conds[i], refiner_neg_cond, 1 / weight)[0]
        # return
        extra_pnginfo['PromptWithStyle'] = prompt_with_style
        return (base_model, {"samples":latent},
                sdxl_pos_cond, sdxl_neg_cond,
                refiner_pos_cond, refiner_neg_cond,
                pos_prompt_, neg_prompt_, {'extra_pnginfo': extra_pnginfo})

class StyleConditioner:
    @classmethod
    def INPUT_TYPES(s):
        s.styles, s.pos_style, s.neg_style = read_styles()
        return {"required": {"style": (s.styles,),"strength": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.1}),
                             "positive_cond_base": ("CONDITIONING",), "negative_cond_base": ("CONDITIONING",),
                             "positive_cond_refiner": ("CONDITIONING",), "negative_cond_refiner": ("CONDITIONING",),
                             "base_clip": ("CLIP",), "refiner_clip": ("CLIP",),
                             }
        }

    RETURN_TYPES = ('CONDITIONING','CONDITIONING','CONDITIONING','CONDITIONING',)
    RETURN_NAMES = ('base_pos_cond','base_neg_cond','refiner_pos_cond','refiner_neg_cond',)
    FUNCTION = 'add_style'
    CATEGORY = 'Mikey/Conditioning'

    def add_style(self, style, strength, positive_cond_base, negative_cond_base, positive_cond_refiner, negative_cond_refiner, base_clip, refiner_clip):
        pos_prompt = self.pos_style[style]
        neg_prompt = self.neg_style[style]
        pos_prompt = pos_prompt.replace('{prompt}', '')
        neg_prompt = neg_prompt.replace('{prompt}', '')
        # encode the style prompt
        positive_cond_base_new = CLIPTextEncodeSDXL.encode(self, base_clip, 1024, 1024, 0, 0, 1024, 1024, pos_prompt, pos_prompt)[0]
        negative_cond_base_new = CLIPTextEncodeSDXL.encode(self, base_clip, 1024, 1024, 0, 0, 1024, 1024, neg_prompt, neg_prompt)[0]
        positive_cond_refiner_new = CLIPTextEncodeSDXLRefiner.encode(self, refiner_clip, 6, 4096, 4096, pos_prompt)[0]
        negative_cond_refiner_new = CLIPTextEncodeSDXLRefiner.encode(self, refiner_clip, 2.5, 4096, 4096, neg_prompt)[0]
        # average the style prompt with the existing conditioning
        positive_cond_base = ConditioningAverage.addWeighted(self, positive_cond_base_new, positive_cond_base, strength)[0]
        negative_cond_base = ConditioningAverage.addWeighted(self, negative_cond_base_new, negative_cond_base, strength)[0]
        positive_cond_refiner = ConditioningAverage.addWeighted(self, positive_cond_refiner_new, positive_cond_refiner, strength)[0]
        negative_cond_refiner = ConditioningAverage.addWeighted(self, negative_cond_refiner_new, negative_cond_refiner, strength)[0]

        return (positive_cond_base, negative_cond_base, positive_cond_refiner, negative_cond_refiner,)

def calculate_image_complexity(image):
    pil_image = tensor2pil(image)
    image = np.array(pil_image)

    # Convert image to grayscale for edge detection, GLCM, and entropy
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    # 1. Edge Detection
    edges = cv2.Canny(gray, 100, 200)
    edge_density = float(np.sum(edges)) / (image.shape[0] * image.shape[1])

    # 2. Texture Analysis using GLCM
    glcm = graycomatrix(gray, [1], [0], 256, symmetric=True, normed=True)

    max_contrast = 100  # adjust based on your typical range if needed
    contrast = max_contrast - graycoprops(glcm, 'contrast')[0, 0]
    contrast = contrast / max_contrast  # Normalize contrast

    dissimilarity = graycoprops(glcm, 'dissimilarity')[0, 0]

    # Normalize dissimilarity (assuming an arbitrary range of 0 to 10, adjust if needed)
    dissimilarity /= 10

    # 3. Color Variability
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hue_std = np.std(hsv[:, :, 0]) / 180  # Normalize: hue values range from 0 to 180 in OpenCV
    saturation_std = np.std(hsv[:, :, 1]) / 255  # Normalize: saturation values range from 0 to 255
    value_std = np.std(hsv[:, :, 2]) / 255  # Normalize: value (brightness) values range from 0 to 255

    # 4. Entropy
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
    hist /= hist.sum()
    entropy = -np.sum(hist*np.log2(hist + np.finfo(float).eps))  # Adding epsilon to avoid log(0)
    # Normalize entropy assuming an arbitrary range of 0 to 8 (adjust if needed)
    entropy /= 8

    # Compute a combined complexity score.
    # You can adjust the weights as per the importance of each feature.
    complexity = edge_density + contrast + dissimilarity + hue_std + saturation_std + value_std + entropy

    return complexity

class MikeySampler:
    @classmethod
    def INPUT_TYPES(s):

        return {"required": {"base_model": ("MODEL",), "refiner_model": ("MODEL",), "samples": ("LATENT",), "vae": ("VAE",),
                             "positive_cond_base": ("CONDITIONING",), "negative_cond_base": ("CONDITIONING",),
                             "positive_cond_refiner": ("CONDITIONING",), "negative_cond_refiner": ("CONDITIONING",),
                             "model_name": (folder_paths.get_filename_list("upscale_models"), ),
                             "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                             "upscale_by": ("FLOAT", {"default": 1.0, "min": 0.1, "max": 10.0, "step": 0.1}),
                             "hires_strength": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.1})}}

    RETURN_TYPES = ('LATENT',)
    FUNCTION = 'run'
    CATEGORY = 'Mikey/Sampling'

    def adjust_start_step(self, image_complexity, hires_strength=1.0):
        image_complexity /= 10
        if image_complexity > 1:
            image_complexity = 1
        image_complexity = min([0.55, image_complexity]) * hires_strength
        return min([16, 16 - int(round(image_complexity * 16,0))])

    def run(self, seed, base_model, refiner_model, vae, samples, positive_cond_base, negative_cond_base,
            positive_cond_refiner, negative_cond_refiner, model_name, upscale_by=1.0, hires_strength=1.0):
        image_scaler = ImageScale()
        vaeencoder = VAEEncode()
        vaedecoder = VAEDecode()
        uml = UpscaleModelLoader()
        upscale_model = uml.load_model(model_name)[0]
        iuwm = ImageUpscaleWithModel()
        # common_ksampler(model, seed, steps, cfg, sampler_name, scheduler, positive, negative, latent, denoise=1.0,
        # disable_noise=False, start_step=None, last_step=None, force_full_denoise=False)
        # step 1 run base model
        sample1 = common_ksampler(base_model, seed, 25, 6.5, 'dpmpp_2s_ancestral', 'simple', positive_cond_base, negative_cond_base, samples,
                                  start_step=0, last_step=18, force_full_denoise=False)[0]
        # step 2 run refiner model
        sample2 = common_ksampler(refiner_model, seed, 30, 3.5, 'dpmpp_2m', 'simple', positive_cond_refiner, negative_cond_refiner, sample1,
                                  disable_noise=True, start_step=21, force_full_denoise=True)[0]
        # step 3 upscale
        pixels = vaedecoder.decode(vae, sample2)[0]
        org_width, org_height = pixels.shape[2], pixels.shape[1]
        img = iuwm.upscale(upscale_model, image=pixels)[0]
        upscaled_width, upscaled_height = int(org_width * upscale_by // 8 * 8), int(org_height * upscale_by // 8 * 8)
        img = image_scaler.upscale(img, 'nearest-exact', upscaled_width, upscaled_height, 'center')[0]
        # Adjust start_step based on complexity
        image_complexity = calculate_image_complexity(img)
        print('Image Complexity:', image_complexity)
        start_step = self.adjust_start_step(image_complexity, hires_strength)
        # encode image
        latent = vaeencoder.encode(vae, img)[0]
        # step 3 run base model
        out = common_ksampler(base_model, seed, 16, 9.5, 'dpmpp_2m_sde', 'karras', positive_cond_base, negative_cond_base, latent,
                                  start_step=start_step, force_full_denoise=True)
        return out

class PromptWithSDXL:
    @classmethod
    def INPUT_TYPES(s):
        s.ratio_sizes, s.ratio_dict = read_ratios()
        return {"required": {"positive_prompt": ("STRING", {"multiline": True, 'default': 'Positive Prompt'}),
                             "negative_prompt": ("STRING", {"multiline": True, 'default': 'Negative Prompt'}),
                             "positive_style": ("STRING", {"multiline": True, 'default': 'Positive Style'}),
                             "negative_style": ("STRING", {"multiline": True, 'default': 'Negative Style'}),
                             "ratio_selected": (s.ratio_sizes,),
                             "batch_size": ("INT", {"default": 1, "min": 1, "max": 64}),
                             "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff})
                             }
        }

    RETURN_TYPES = ('LATENT','STRING','STRING','STRING','STRING','INT','INT','INT','INT',)
    RETURN_NAMES = ('samples','positive_prompt_text_g','negative_prompt_text_g','positive_style_text_l',
                    'negative_style_text_l','width','height','refiner_width','refiner_height',)
    FUNCTION = 'start'
    CATEGORY = 'Mikey'

    def start(self, positive_prompt, negative_prompt, positive_style, negative_style, ratio_selected, batch_size, seed):
        positive_prompt = find_and_replace_wildcards(positive_prompt, seed)
        negative_prompt = find_and_replace_wildcards(negative_prompt, seed)
        width = self.ratio_dict[ratio_selected]["width"]
        height = self.ratio_dict[ratio_selected]["height"]
        latent = torch.zeros([batch_size, 4, height // 8, width // 8])
        refiner_width = width * 4
        refiner_height = height * 4
        return ({"samples":latent},
                str(positive_prompt),
                str(negative_prompt),
                str(positive_style),
                str(negative_style),
                width,
                height,
                refiner_width,
                refiner_height,)

class UpscaleTileCalculator:
    @classmethod
    def INPUT_TYPES(s):
        return {'required': {'image': ('IMAGE',),
                            # 'upscale_by': ('FLOAT', {'default': 1.0, 'min': 0.1, 'max': 10.0, 'step': 0.1}),
                             'tile_resolution': ('INT', {'default': 512, 'min': 1, 'max': 8192, 'step': 8})}}

    RETURN_TYPES = ('IMAGE', 'INT', 'INT')
    RETURN_NAMES = ('image', 'tile_width', 'tile_height')
    FUNCTION = 'calculate'
    CATEGORY = 'Mikey/Image'

    def upscale(self, image, upscale_method, width, height, crop):
        samples = image.movedim(-1,1)
        s = comfy.utils.common_upscale(samples, width, height, upscale_method, crop)
        s = s.movedim(1,-1)
        return (s,)

    def resize(self, image, width, height, upscale_method, crop):
        w, h = find_latent_size(image.shape[2], image.shape[1])
        print('Resizing image from {}x{} to {}x{}'.format(image.shape[2], image.shape[1], w, h))
        img = self.upscale(image, upscale_method, w, h, crop)[0]
        return (img, )

    def calculate(self, image, tile_resolution):
        width, height = image.shape[2], image.shape[1]
        tile_width, tile_height = find_tile_dimensions(width, height, 1.0, tile_resolution)
        print('Tile width: ' + str(tile_width), 'Tile height: ' + str(tile_height))
        return (image, tile_width, tile_height)

""" Deprecated Nodes """

class VAEDecode6GB:
    """ deprecated. update comfy to fix issue. """
    @classmethod
    def INPUT_TYPES(s):
        return {'required': {'vae': ('VAE',),
                             'samples': ('LATENT',)}}
    RETURN_TYPES = ('IMAGE',)
    FUNCTION = 'decode'
    #CATEGORY = 'Mikey/Latent'

    def decode(self, vae, samples):
        unload_model()
        soft_empty_cache()
        return (vae.decode(samples['samples']), )

NODE_CLASS_MAPPINGS = {
    'Wildcard Processor': WildcardProcessor,
    'Empty Latent Ratio Select SDXL': EmptyLatentRatioSelector,
    'Empty Latent Ratio Custom SDXL': EmptyLatentRatioCustom,
    'Save Image With Prompt Data': SaveImagesMikey,
    'Resize Image for SDXL': ResizeImageSDXL,
    'Upscale Tile Calculator': UpscaleTileCalculator,
    'Batch Resize Image for SDXL': BatchResizeImageSDXL,
    'Prompt With Style': PromptWithStyle,
    'Prompt With Style V2': PromptWithStyleV2,
    'Prompt With Style V3': PromptWithStyleV3,
    'Prompt With SDXL': PromptWithSDXL,
    'Style Conditioner': StyleConditioner,
    'Mikey Sampler': MikeySampler,
    'AddMetaData': AddMetaData,
    'SaveMetaData': SaveMetaData,
    'HaldCLUT ': HaldCLUT,
    'VAE Decode 6GB SDXL (deprecated)': VAEDecode6GB,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    'Wildcard Processor': 'Wildcard Processor (Mikey)',
    'Empty Latent Ratio Select SDXL': 'Empty Latent Ratio Select SDXL (Mikey)',
    'Empty Latent Ratio Custom SDXL': 'Empty Latent Ratio Custom SDXL (Mikey)',
    'Save Image With Prompt Data': 'Save Image With Prompt Data (Mikey)',
    'Resize Image for SDXL': 'Resize Image for SDXL (Mikey)',
    'Upscale Tile Calculator': 'Upscale Tile Calculator (Mikey)',
    'Batch Resize Image for SDXL': 'Batch Resize Image for SDXL (Mikey)',
    'Prompt With Style': 'Prompt With Style V1 (Mikey)',
    'Prompt With Style V2': 'Prompt With Style V2 (Mikey)',
    'Prompt With Style V3': 'Prompt With Style (Mikey)',
    'Prompt With SDXL': 'Prompt With SDXL (Mikey)',
    'Style Conditioner': 'Style Conditioner (Mikey)',
    'Mikey Sampler': 'Mikey Sampler',
    'AddMetaData': 'AddMetaData (Mikey)',
    'SaveMetaData': 'SaveMetaData (Mikey)',
    'HaldCLUT': 'HaldCLUT (Mikey)',
    'VAE Decode 6GB SDXL (deprecated)': 'VAE Decode 6GB SDXL (deprecated) (Mikey)',
}