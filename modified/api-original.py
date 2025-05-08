from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import uuid
from datetime import datetime
import os
import json
import shutil
from typing import Dict, Any
from azure_ocr import Inferencer, save_to_markdown
from ocr_json import convert_to_json, read_markdown_file, extract_ocr_text

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

@app.post("/documents")
async def process_document(file: UploadFile = File(...)):
    try:
        # Generate UUID and get current date
        file_id = str(uuid.uuid4())
        current_date = datetime.now().strftime("%Y-%m-%d")
        
        # Get file extension
        _, ext = os.path.splitext(file.filename)
        
        # Create temporary file paths
        temp_file_path = os.path.join(TEMP_DIR, f"temp_{file_id}{ext}")
        temp_md_path = os.path.join(TEMP_DIR, f"temp_{file_id}.md")
        temp_json_path = os.path.join(TEMP_DIR, f"temp_{file_id}.json")
        
        # Save uploaded file to temporary location
        with open(temp_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        # Initialize OCR inferencer and process image
        inferencer = Inferencer()
        ocr_results = inferencer.run_inference(temp_file_path)
        
        # Save OCR results to markdown
        save_to_markdown(ocr_results, temp_md_path, temp_file_path)
        
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
        shutil.move(temp_file_path, final_file_path)
        shutil.move(temp_md_path, final_md_path)
        
        # Save JSON to final location
        with open(final_json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        
        # Return response
        return {
            "status": "success",
            "data": json_data,
            "file_info": {
                "original_name": file.filename,
                "patient_name": patient_name,
                "file_id": file_id,
                "date": current_date
            }
        }
        
    except Exception as e:
        # Clean up temporary files in case of error
        for temp_file in [temp_file_path, temp_md_path, temp_json_path]:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        
        return {"status": "error", "message": str(e)}
    
    finally:
        # Clean up temporary files after successful processing
        for temp_file in [temp_file_path, temp_md_path, temp_json_path]:
            if os.path.exists(temp_file):
                os.remove(temp_file)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8007, reload=True)