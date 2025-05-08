from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import uuid
from datetime import datetime
import os
import logging
import json
import shutil
from typing import Dict, Any, List, Tuple, Optional
from azure_ocr import Inferencer, save_to_markdown
from convert_to_json import convert_to_json, read_markdown_file, extract_ocr_text
from pdf2image import convert_from_path
from PIL import Image
from fastapi.responses import StreamingResponse
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from starlette.background import BackgroundTask
import io
import subprocess
import sys

app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


TEMP_DIR = "temp_files"
OUTPUTS_DIR = "outputs"
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(OUTPUTS_DIR, exist_ok=True)

MICLINIC_UPLOAD_DIR = Path("./uploads")
MICLINIC_UPLOAD_DIR.mkdir(exist_ok=True)

ARCHIVE_ROOT = Path("./archives")
ARCHIVE_ROOT.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Utility cleanup used by /v1/edited endpoint
# ---------------------------------------------------------------------------

def _cleanup_after_send(paths: List[Path]):
    """Background task: move each sent file into ./archives/<YYYY-MM-DD>/.

    Keeps processed records for auditing while preventing the worker from
    re-processing the same payload on the next poll.
    """
    date_dir = ARCHIVE_ROOT / datetime.now().strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    for p in paths:
        try:
            target = date_dir / p.name
            shutil.move(str(p), target)
            logger.info(f"Archived processed file → {target}")
        except Exception as exc:
            logger.error(f"Failed to archive {p}: {exc}")

# ---------------------------------------------------------------------------
# Helper functions for the new EHR upload endpoints
# ---------------------------------------------------------------------------

def miclinic_get_latest_files() -> Tuple[Optional[Path], Optional[Path]]:
    """Return newest JSON and PDF uploaded for EHR processing."""
    json_files = list(MICLINIC_UPLOAD_DIR.glob("*.json"))
    pdf_files = list(MICLINIC_UPLOAD_DIR.glob("*.pdf"))

    latest_json = max(json_files, key=lambda p: p.stat().st_mtime, default=None)
    latest_pdf = max(pdf_files, key=lambda p: p.stat().st_mtime, default=None)

    return latest_json, latest_pdf

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

def save_file(source_path: str, dest_path: str, copy_instead_of_move: bool = False) -> None:
    """Save a file by either copying or moving it."""
    if copy_instead_of_move:
        shutil.copy(source_path, dest_path)
    else:
        shutil.move(source_path, dest_path)

def is_pdf(filename: str) -> bool:
    """Check if a file is a PDF based on extension."""
    return filename.lower().endswith('.pdf')

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

def process_image(image_path: str, output_md_path: str) -> Optional[Dict[str, Any]]:
    """Process a single image through OCR and JSON conversion."""
    try:
        # OCR processing
        inferencer = Inferencer()
        ocr_results = inferencer.run_inference(image_path)
        save_to_markdown(ocr_results, output_md_path, image_path)
        
        # Convert to JSON
        markdown_content = read_markdown_file(output_md_path)
        ocr_text = extract_ocr_text(markdown_content)
        json_data = convert_to_json(ocr_text, os.path.basename(output_md_path))
        
        return json_data
    except Exception as e:
        print(f"Error processing image {image_path}: {str(e)}")
        return None

def save_json(json_data: Dict[str, Any], output_path: str) -> None:
    """Save JSON data to a file."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)

def build_response(
    status: str, 
    data: Dict[str, Any], 
    file_info: Dict[str, Any]
) -> Dict[str, Any]:
    """Build a standardized API response."""
    return {
        "status": status,
        "data": data,
        "file_info": file_info
    }

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
        
        # Get the output directory
        date_dir = create_date_directory(current_date)
        
        # Process file based on type
        if is_pdf(original_filename):
            # Convert PDF to JPEG
            image_paths = convert_pdf_to_jpeg(temp_upload_path)
            temp_files.extend(image_paths)
            
            # Track all JSON results
            all_json_data = []
            all_md_paths = []
            
            # Process each page
            for i, image_path in enumerate(image_paths):
                # Set up temporary paths
                temp_md_path = os.path.join(TEMP_DIR, f"temp_{file_id}_page_{i+1}.md")
                temp_json_path = os.path.join(TEMP_DIR, f"temp_{file_id}_page_{i+1}.json")
                temp_files.extend([temp_md_path])
                all_md_paths.append(temp_md_path)
                
                # Process the image
                json_data = process_image(image_path, temp_md_path)
                
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
            
            # Save the original PDF
            final_pdf_path = os.path.join(date_dir, f"{patient_name}.pdf")
            save_file(temp_upload_path, final_pdf_path, copy_instead_of_move=True)
            
            # Save all images and markdown files
            image_file_paths = []
            md_file_paths = []
            
            for i, (image_path, md_path) in enumerate(zip(image_paths, all_md_paths)):
                # Save image
                final_image_path = os.path.join(date_dir, f"{patient_name}_page_{i+1}.jpg")
                save_file(image_path, final_image_path, copy_instead_of_move=True)
                image_file_paths.append(final_image_path)
                
                # Save markdown
                final_md_path = os.path.join(date_dir, f"{patient_name}_page_{i+1}.md")
                save_file(md_path, final_md_path, copy_instead_of_move=True)
                md_file_paths.append(final_md_path)
            
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
                save_json(combined_json, final_json_path)
                return_data = combined_json
            elif len(all_json_data) == 1:
                # For single-page PDFs, save the single JSON
                save_json(json_data, final_json_path)
                return_data = json_data
            else:
                # No JSON data available
                return_data = {"error": "No OCR data could be extracted"}
                save_json(return_data, final_json_path)
            
            return build_response(
                status="success",
                data=return_data,
                file_info={
                    "original_name": original_filename,
                    "patient_name": patient_name,
                    "file_id": file_id,
                    "date": current_date,
                }
            )
            
        else:
            # Process as regular image
            temp_md_path = os.path.join(TEMP_DIR, f"temp_{file_id}.md")
            temp_files.append(temp_md_path)
            
            # Process the image
            json_data = process_image(temp_upload_path, temp_md_path)
            
            if not json_data:
                return build_response(
                    status="error",
                    data={"error": "Failed to extract OCR data from image"},
                    file_info={"original_name": original_filename}
                )
            
            # Extract patient name
            patient_name = get_patient_name_from_json(json_data)
            
            # Create final file paths
            final_file_path = os.path.join(date_dir, f"{patient_name}{ext}")
            final_md_path = os.path.join(date_dir, f"{patient_name}.md")
            final_json_path = os.path.join(date_dir, f"{patient_name}.json")
            
            # Save files
            save_file(temp_upload_path, final_file_path, copy_instead_of_move=True)
            save_file(temp_md_path, final_md_path, copy_instead_of_move=True)
            save_json(json_data, final_json_path)
            
            # Return response
            return build_response(
                status="success",
                data=json_data,
                file_info={
                    "original_name": original_filename,
                    "patient_name": patient_name,
                    "file_id": file_id,
                    "date": current_date,
                }
            )
        
    except Exception as e:
        return build_response(
            status="error",
            data={"error": str(e)},
            file_info={"original_name": original_filename if 'original_filename' in locals() else "unknown"}
        )
    
    finally:
        # Clean up all temporary files
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                if os.path.isdir(temp_file):
                    shutil.rmtree(temp_file)
                else:
                    os.remove(temp_file)

def _run_automation_worker():
    """Background task: invoke automate_upload.py after new files arrive.

    Runs the Playwright form-filling robot in a best-effort manner. Errors are
    only logged so the API response remains successful for the uploader.
    """
    try:
        subprocess.run([sys.executable, "automate_upload.py"], check=False)
    except Exception as exc:
        logger.error(f"Failed to start automation worker: {exc}")

@app.post("/upload")
async def miclinic_upload_files(background_tasks: BackgroundTasks, files: List[UploadFile] = File(...)):
    """Receive JSON and PDF destined for the EHR automation worker.
    Saves them under ./uploads with timestamped names so the worker can pick
    up the newest pair via /v1/edited.
    """
    # ------------------------------------------------------------------
    # 1) First read each file into memory so we can parse the JSON (to get
    #    the patient name) before committing the filenames to disk.
    # ------------------------------------------------------------------
    in_memory: List[Tuple[UploadFile, bytes]] = []

    for file in files:
        # Validate types early
        if not file.filename.lower().endswith((".json", ".pdf")):
            logger.warning(f"Invalid file type uploaded to /upload: {file.filename}")
            raise HTTPException(status_code=400, detail=f"File {file.filename} must be .json or .pdf")

        content = await file.read()
        in_memory.append((file, content))

    # ------------------------------------------------------------------
    # 2) Persist the files with clean names: <YYYYMMDD_HHMMSS>.<ext>
    # ------------------------------------------------------------------
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    saved_files: List[str] = []

    for original_file, content in in_memory:
        base, ext = os.path.splitext(original_file.filename)
        clean_base = base
        unique_name = f"{clean_base}_{ts}{ext}"
        dest_path = MICLINIC_UPLOAD_DIR / unique_name

        try:
            dest_path.write_bytes(content)
            saved_files.append(unique_name)
            logger.info(f"/ehr/upload saved file: {unique_name}")
        except Exception as exc:
            logger.error(f"Failed saving file {original_file.filename}: {exc}")
            raise HTTPException(status_code=500, detail=f"Failed to save {original_file.filename}: {exc}")

    # ------------------------------------------------------------------
    # 3) Kick off the automation worker in the background so that the visit
    #    is filled in MiClinic without blocking the uploader.
    # ------------------------------------------------------------------
    background_tasks.add_task(_run_automation_worker)

    return {"message": "Files uploaded successfully", "filenames": saved_files}

@app.get("/v1/edited")
async def miclinic_json():
    """Return latest JSON and PDF as multipart/form-data for the automation worker."""
    json_file, pdf_file = miclinic_get_latest_files()

    if not json_file and not pdf_file:
        logger.warning("/v1/edited: No JSON or PDF available in uploads directory")
        raise HTTPException(status_code=404, detail="No JSON or PDF files found")

    msg = MIMEMultipart()

    if json_file:
        try:
            with json_file.open("rb") as f:
                part = MIMEBase("application", "json")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{json_file.name}"')
            part.add_header("Content-Type", "application/json")
            msg.attach(part)
            logger.info(f"/v1/edited attached JSON: {json_file.name}")
        except Exception as exc:
            logger.error(f"Could not attach JSON file {json_file.name}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    if pdf_file:
        try:
            with pdf_file.open("rb") as f:
                part = MIMEBase("application", "pdf")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{pdf_file.name}"')
            part.add_header("Content-Type", "application/pdf")
            msg.attach(part)
            logger.info(f"/v1/edited attached PDF: {pdf_file.name}")
        except Exception as exc:
            logger.error(f"Could not attach PDF file {pdf_file.name}: {exc}")
            raise HTTPException(status_code=500, detail=str(exc))

    boundary = msg.get_boundary()
    buffer = io.BytesIO()
    buffer.write(msg.as_bytes())
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type=f"multipart/form-data; boundary={boundary}",
        headers={"Content-Disposition": "attachment"},
        background=BackgroundTask(_cleanup_after_send, [p for p in (json_file, pdf_file) if p]),
    )

@app.post("/v1/edited")
async def receive_edited(
    json_file: UploadFile = File(..., alias="json"),           # required
    file1: UploadFile | None = File(None),       # optional
    file2: UploadFile | None = File(None),       # optional
):
    # --- handle the JSON report -----------------------------------------
    raw_json = await json_file.read()
    data     = json.loads(raw_json)              # parse if you need it
    (MICLINIC_UPLOAD_DIR / json_file.filename).write_bytes(raw_json)

    # --- handle the extra PDFs/images -----------------------------------
    for f in (file1, file2):
        if f:                       # skip None
            (MICLINIC_UPLOAD_DIR / f.filename).write_bytes(await f.read())

    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8007, reload=True)