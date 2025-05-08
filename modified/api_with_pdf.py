from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import uuid
from datetime import datetime
import os
import json
import shutil
from typing import Dict, Any, List
from azure_ocr import Inferencer, save_to_markdown
from ocr_json import convert_to_json, read_markdown_file, extract_ocr_text
from pdf2image import convert_from_path
from PIL import Image

app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create necessary directories if they don't exist
TEMP_DIR = "temp_files"
OUTPUTS_DIR = "outputs"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)

def get_patient_name_from_json(json_data: Dict[str, Any]) -> str:
    """Extract and format patient name from JSON data."""
    try:
        full_name = json_data["ocr_contents"]["insured"]["insuredName"]
        # Split name into parts
        name_parts = full_name.split()
        if len(name_parts) >= 3:
            return f"{name_parts[0][0]}_{name_parts[1][0]}_{name_parts[-1]}"
        return full_name.replace(" ", "_")
    except (KeyError, IndexError):
        return str(uuid.uuid4())[:8]  # Fallback to UUID if name extraction fails

def create_date_directory(date_str: str) -> str:
    """Create and return path to date-based directory."""
    date_dir = os.path.join(OUTPUTS_DIR, date_str)
    os.makedirs(date_dir, exist_ok=True)
    return date_dir

def convert_pdf_to_jpeg(pdf_path: str) -> List[str]:
    """Convert PDF to JPEG images and return paths to the images."""
    # Create a directory for the images
    pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
    images_dir = os.path.join(TEMP_DIR, f"{pdf_name}_images")
    os.makedirs(images_dir, exist_ok=True)
    
    # Convert PDF to images
    images = convert_from_path(pdf_path, dpi=300)
    image_paths = []
    
    # Save each image
    for i, image in enumerate(images):
        image_path = os.path.join(images_dir, f"page_{i+1}.jpg")
        image.save(image_path, "JPEG", quality=95)
        image_paths.append(image_path)
    
    return image_paths

def is_pdf(filename: str) -> bool:
    """Check if a file is a PDF based on extension."""
    return filename.lower().endswith('.pdf')

@app.post("/documents")
async def process_document(file: UploadFile = File(...)):
    # Create temporary files to clean up later
    temp_files = []
    
    try:
        # Generate UUID and get current date
        file_id = str(uuid.uuid4())
        current_date = datetime.now().strftime("%Y-%m-%d")
        
        # Get file extension
        original_filename = file.filename
        _, ext = os.path.splitext(original_filename)
        
        # Create temporary file path for the uploaded file
        temp_upload_path = os.path.join(TEMP_DIR, f"upload_{file_id}{ext}")
        temp_files.append(temp_upload_path)
        
        # Save uploaded file to temporary location
        with open(temp_upload_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Process file based on type
        if is_pdf(original_filename):
            # Convert PDF to JPEG
            image_paths = convert_pdf_to_jpeg(temp_upload_path)
            temp_files.extend(image_paths)
            
            # Track all JSON results
            all_json_data = []
            
            # Process each page
            for i, image_path in enumerate(image_paths):
                # Process the image
                temp_md_path = os.path.join(TEMP_DIR, f"temp_{file_id}_page_{i+1}.md")
                temp_json_path = os.path.join(TEMP_DIR, f"temp_{file_id}_page_{i+1}.json")
                temp_files.extend([temp_md_path, temp_json_path])
                
                # OCR processing
                inferencer = Inferencer()
                ocr_results = inferencer.run_inference(image_path)
                save_to_markdown(ocr_results, temp_md_path, image_path)
                
                # Convert to JSON
                markdown_content = read_markdown_file(temp_md_path)
                ocr_text = extract_ocr_text(markdown_content)
                json_data = convert_to_json(ocr_text, os.path.basename(temp_md_path))
                
                if json_data:
                    all_json_data.append(json_data)
            
            # Use the first page's JSON for patient name if available
            if all_json_data:
                json_data = all_json_data[0]
                patient_name = get_patient_name_from_json(json_data)
            else:
                # Fallback if no JSON data available
                patient_name = str(uuid.uuid4())[:8]
                json_data = {}
            
            # Create date directory and final file paths
            date_dir = create_date_directory(current_date)
            
            # Save the original PDF
            final_pdf_path = os.path.join(date_dir, f"{patient_name}.pdf")
            shutil.copy(temp_upload_path, final_pdf_path)
            
            # Save all images
            image_file_paths = []
            for i, image_path in enumerate(image_paths):
                final_image_path = os.path.join(date_dir, f"{patient_name}_page_{i+1}.jpg")
                shutil.copy(image_path, final_image_path)
                image_file_paths.append(final_image_path)
            
            # Save the combined JSON or first page JSON
            final_json_path = os.path.join(date_dir, f"{patient_name}.json")
            if len(all_json_data) > 1:
                # For multi-page PDFs, save a summary JSON
                combined_json = {
                    "file_name": original_filename,
                    "patient_name": patient_name,
                    "page_count": len(all_json_data),
                    "pages": all_json_data
                }
                with open(final_json_path, "w", encoding="utf-8") as f:
                    json.dump(combined_json, f, indent=2, ensure_ascii=False)
                
                return_data = combined_json
            elif len(all_json_data) == 1:
                # For single-page PDFs, save the single JSON
                with open(final_json_path, "w", encoding="utf-8") as f:
                    json.dump(json_data, f, indent=2, ensure_ascii=False)
                
                return_data = json_data
            else:
                # No JSON data available
                return_data = {"error": "No OCR data could be extracted"}
            
            return {
                "status": "success",
                "data": return_data,
                "file_info": {
                    "original_name": original_filename,
                    "patient_name": patient_name,
                    "file_id": file_id,
                    "date": current_date,
                }
            }
            
        else:
            # Process as regular image
            temp_md_path = os.path.join(TEMP_DIR, f"temp_{file_id}.md")
            temp_json_path = os.path.join(TEMP_DIR, f"temp_{file_id}.json")
            temp_files.extend([temp_md_path, temp_json_path])
            
            # Initialize OCR inferencer and process image
            inferencer = Inferencer()
            ocr_results = inferencer.run_inference(temp_upload_path)
            
            # Save OCR results to markdown
            save_to_markdown(ocr_results, temp_md_path, temp_upload_path)
            
            # Read markdown and convert to JSON
            markdown_content = read_markdown_file(temp_md_path)
            ocr_text = extract_ocr_text(markdown_content)
            json_data = convert_to_json(ocr_text, os.path.basename(temp_md_path))
            
            # Extract patient name and format filenames
            patient_name = get_patient_name_from_json(json_data)
            
            # Create date directory and final file paths
            date_dir = create_date_directory(current_date)
            final_file_path = os.path.join(date_dir, f"{patient_name}{ext}")
            final_md_path = os.path.join(date_dir, f"{patient_name}.md")
            final_json_path = os.path.join(date_dir, f"{patient_name}.json")
            
            # Move files to final location
            shutil.copy(temp_upload_path, final_file_path)
            shutil.copy(temp_md_path, final_md_path)
            
            # Save JSON to final location
            with open(final_json_path, "w", encoding="utf-8") as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            
            # Return response
            return {
                "status": "success",
                "data": json_data,
                "file_info": {
                    "original_name": original_filename,
                    "patient_name": patient_name,
                    "file_id": file_id,
                    "date": current_date,
                }
            }
        
    except Exception as e:
        return {"status": "error", "message": str(e)}
    
    finally:
        # Clean up all temporary files
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                if os.path.isdir(temp_file):
                    shutil.rmtree(temp_file)
                else:
                    os.remove(temp_file)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8007, reload=True)