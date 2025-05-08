import time
import cv2
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from azure.cognitiveservices.vision.computervision.models import (
    OperationStatusCodes,
)
from msrest.authentication import CognitiveServicesCredentials
from PIL import Image
import numpy as np
from dotenv import load_dotenv
import os, logging, json
import requests
from openai import OpenAI

load_dotenv()

logger = logging.getLogger(__name__)

SUBSCRIPTION_KEY = os.getenv("AZURE_KEY")
ENDPOINT = os.getenv("AZURE_ENDPOINT")
REGION = os.getenv("AZURE_REGION")

def load_image(filename: str) -> Image:
    image = Image.open(filename)
    # Convert Image to Grayscale
    image = image.convert("L")
    # Apply threshold on Image
    image = image.point(lambda p: 255 if p > 70 else 0)
    return image


def load_image_cv2(filename: str):
    image = cv2.imread(filename)
    # cv2.cvtColor is applied over the image input with applied parameters
    # to convert the image in grayscale
    image = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # Applying Otsu thresholding as an extra flag in binary thresholding
    ret, thresh = cv2.threshold(image, 120, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def group_lines(ocr_results, use_paddle=False):
    lines = []
    heights = []

    for box in ocr_results:
        coords = box[0]
        y_min = min(int(coords[0][1]), int(coords[1][1]))  # Top Y-coordinate
        y_max = max(int(coords[2][1]), int(coords[3][1]))  # Bottom Y-coordinate
        text = box[1] if not use_paddle else box[1][0]
        height = y_max - y_min  # Height of the box
        heights.append(height)
        lines.append({"y_min": y_min, "y_max": y_max, "text": text})

    avg_height = np.mean(heights)
    dynamic_threshold = avg_height * 0.8  # Adjust the multiplier as needed

    # Sort lines by their Y-coordinates
    lines.sort(key=lambda x: x["y_min"])

    # Group lines based on overlapping Y-coordinates
    grouped_lines = []
    current_group = [lines[0]["text"]]
    current_y_range = [lines[0]["y_min"], lines[0]["y_max"]]

    for line in lines[1:]:
        y_min, y_max = line["y_min"], line["y_max"]

        # Check if the line overlaps with the current group
        if current_y_range[1] - y_min >= dynamic_threshold:
            current_group.append(line["text"])
            current_y_range[1] = max(current_y_range[1], y_max)
        else:
            # Start a new group
            grouped_lines.append(" ".join(current_group))
            current_group = [line["text"]]
            current_y_range = [y_min, y_max]

    # Append the last group
    grouped_lines.append(" ".join(current_group))

    return "\n".join(grouped_lines) if grouped_lines else ""



class Inferencer:
    """A class representing OCR inference."""

    def __init__(self, use_gpu: bool = True) -> None:
        self.use_gpu = use_gpu

        self.langs = ["en", "ar"]

        self.computervision_client = ComputerVisionClient(
            ENDPOINT, CognitiveServicesCredentials(SUBSCRIPTION_KEY)
        )

    def run_azure_ocr(self, filename: str):
        """
        Run the inference using azure OCR.
        :param filename: The filename of the image
        :param card: ID card to run post processing accordingly
        """
        print("Running azure OCR!")
        # Open the image
        read_image = open(filename, "rb")
        # Call API with image and raw response (allows you to get the operation location)
        read_response = self.computervision_client.read_in_stream(
            read_image, 
            raw=True,
            # Let Azure auto-detect the language
            language="",
            read_endpoint="v2.1"
        )
        # Get the operation location (URL with ID as last appendage)
        read_operation_location = read_response.headers["Operation-Location"]
        # Take the ID off and use to get results
        operation_id = read_operation_location.split("/")[-1]
        # Call the "GET" API and wait for the retrieval of the results
        while True:
            read_result = self.computervision_client.get_read_result(operation_id)
            if read_result.status.lower() not in ["notstarted", "running"]:
                break
            print("Waiting for result...")
            time.sleep(10)

        # Print the detected text, line by line
        ocr_reults = []
        if read_result.status == OperationStatusCodes.succeeded:
            for text_result in read_result.analyze_result.read_results:
                for line in text_result.lines:
                    line_words = []
                    for word in line.words:
                        # Preserve the original text without any modification
                        # This will keep Arabic text intact
                        line_words.append(word.text)
                    ocr_reults.append(line_words)
        read_image.close()
        print(ocr_reults)
        return ocr_reults
    

    def run_inference(self, filename: str):
        """Function to run the inference"""
        return self.run_azure_ocr(filename)


def save_to_markdown(ocr_results, output_file, image_path=None):
    """
    Save OCR results to a markdown file.
    
    Args:
        ocr_results: The OCR results to save
        output_file: Path to the markdown file to create
        image_path: Optional path to the source image
    """
    with open(output_file, "w", encoding="utf-8") as f:
        # Write markdown header
        f.write("# OCR Results\n\n")
        
        # Add timestamp
        f.write(f"*Generated on: {time.strftime('%Y-%m-%d %H:%M:%S')}*\n\n")
        
        # Add source image information if provided
        if image_path:
            f.write(f"**Source Image:** `{image_path}`\n\n")
        
        # Add OCR results
        f.write("## Detected Text\n\n")
        
        if isinstance(ocr_results, list) and len(ocr_results) > 0:
            # Handle Google Vision API results
            if isinstance(ocr_results[0], tuple) and len(ocr_results[0]) >= 1:
                # Full text is typically the first entry's first element
                if ocr_results[0][0]:
                    f.write("### Complete Text\n\n")
                    f.write("```\n")
                    f.write(ocr_results[0][0])
                    f.write("\n```\n\n")
                
                # Add individual text entries
                if len(ocr_results) > 1:
                    f.write("### Individual Text Elements\n\n")
                    for i, result in enumerate(ocr_results[1:], 1):
                        if isinstance(result, tuple) and len(result) >= 1:
                            f.write(f"- Item {i}: `{result[0]}`\n")
            else:
                # Generic list handling
                f.write("```\n")
                for item in ocr_results:
                    f.write(f"{str(item)}\n")
                f.write("```\n\n")
        else:
            # Handle string results
            f.write("```\n")
            f.write(str(ocr_results))
            f.write("\n```\n\n")
        
        f.write("## Processing Information\n\n")
        f.write("- Processing Time: Not measured\n\n")
        
        f.write("---\n")
        f.write("*Generated by OCR test script*")




if __name__ == "__main__":
    inferencer = Inferencer()
    
    # Define input and output paths
    image_path = "outputs/2025-03-20/w_f_abbasia_page_1.jpg"
    output_md = "outputs/2025-03-20/w_f_abbasia_page_1.jpg.md"

    
    # Run OCR with Google Vision API
    text_results = inferencer.run_inference(image_path)
    
    
    # Save results to markdown
    save_to_markdown(text_results, output_md, image_path)
    print(f"OCR results saved to {output_md}")
    
