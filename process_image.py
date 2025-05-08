"""
Module for processing images with improved OCR text extraction
"""
from typing import Optional, Dict, Any
from azure_ocr import Inferencer, save_to_markdown
from ocr_json import convert_to_json, read_markdown_file
from ocr_text_processor import improved_extract_ocr_text

def process_image_with_improved_ocr(image_path: str, output_md_path: str) -> Optional[Dict[str, Any]]:
    """
    Process a single image through OCR and JSON conversion with improved text extraction.
    
    Args:
        image_path: Path to the image file
        output_md_path: Path where markdown output will be saved
        
    Returns:
        JSON data extracted from the image, or None if processing failed
    """
    try:
        # OCR processing
        inferencer = Inferencer()
        ocr_results = inferencer.run_inference(image_path)
        save_to_markdown(ocr_results, output_md_path, image_path)
        
        # Convert to JSON using improved text extraction
        markdown_content = read_markdown_file(output_md_path)
        
        # Use improved OCR text extraction
        ocr_text = improved_extract_ocr_text(markdown_content)
        
        # Convert to structured JSON
        json_data = convert_to_json(ocr_text, os.path.basename(output_md_path))
        
        return json_data
    except Exception as e:
        print(f"Error processing image {image_path}: {str(e)}")
        return None

# If this module is run directly, test it on a sample file
if __name__ == "__main__":
    import os
    import sys
    
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        if os.path.exists(image_path):
            output_md_path = image_path + ".md"
            json_data = process_image_with_improved_ocr(image_path, output_md_path)
            if json_data:
                print("Successfully processed image")
                print(json_data)
            else:
                print("Failed to process image")
        else:
            print(f"Image not found: {image_path}")
    else:
        print("Usage: python process_image.py <image_path>")