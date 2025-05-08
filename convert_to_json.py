import json
import os
import re
from openai import OpenAI
from dotenv import load_dotenv
from prompt import MAIN_PROMPT
from models import StructuredOCR, Language, MedicalFormContent
from typing import Dict, Any, List, Optional, Tuple

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=api_key)

def clean_ocr_text(text: str) -> str:
    """Remove single quotes and commas from OCR text while preserving other punctuation."""
    # Split the text into lines
    lines = text.split('\n')
    cleaned_lines = []
    
    for line in lines:
        if line.startswith('[') and line.endswith(']'):
            # Remove quotes and commas only within square brackets
            inner_text = line[1:-1]  # Remove outer square brackets
            # Remove single quotes and commas, but preserve hyphens and colons
            cleaned_inner = inner_text.replace("'", "").replace(",", " ")
            cleaned_line = f"[{cleaned_inner}]"
        else:
            # For lines without square brackets, just remove quotes and commas
            cleaned_line = line.replace("'", "").replace(",", " ")
        cleaned_lines.append(cleaned_line)
    
    return '\n'.join(cleaned_lines)

def process_checkboxes(text: str) -> str:
    """Process checkbox notation in the text.
    Handle both:
    1. Parenthesis-based checkboxes:
       - Empty -> false
       - Single character -> true
       - Otherwise, keep original text
    2. Explicit Yes/No values:
       - "Yes" -> true
       - "No" -> false
    """
    # List of fields that should be treated as checkboxes
    checkbox_fields = [
        "single", "married", "newVisit", "followUp", "refill", "walkIn", 
        "inpatient", "outpatient", "emergencyCase", "chronic", "congenital", "rta",
        "workRelated", "vaccination", "checkUp", "psychiatric", "infertility", "pregnancy",
        "approved", "notApproved"
    ]
    
    def process_line(line: str) -> str:
        # First handle explicit Yes/No values
        for field in checkbox_fields:
            # Look for pattern like "Referral: Yes" or "Referral Yes"
            pattern = rf"\b{field}:?\s+(Yes|No)\b"
            match = re.search(pattern, line, re.IGNORECASE)
            if match:
                value = match.group(1).lower() == "yes"
                return line.replace(match.group(0), f"{field}: {str(value).lower()}")
        
        # Then handle parenthesis-based checkboxes
        def checkbox_replacement(match):
            content = match.group(1).strip()
            # Get some context before the parenthesis
            pre_context = line[:match.start()].split()[-3:] if match.start() > 0 else []
            
            # Check if any of the preceding words match our checkbox fields
            is_checkbox_field = any(field.lower() in [word.lower() for word in pre_context] 
                                  for field in checkbox_fields)
            
            if is_checkbox_field:
                if not content:
                    return "false"
                elif len(content) == 1:
                    return "true"
            
            return f"({content})"
        
        return re.sub(r'\((.*?)\)', checkbox_replacement, line)
    
    # Process each line separately
    lines = text.split('\n')
    processed_lines = [process_line(line) for line in lines]
    return '\n'.join(processed_lines)

def format_key_values(text: str) -> str:
    """Format key-value pairs consistently."""
    lines = text.split('\n')
    formatted_lines = []
    
    for line in lines:
        if line.startswith('[') and line.endswith(']'):
            # Handle pharmacy-style hyphen separation
            if 'PHARMACY-' in line or 'PHARMACY -' in line:
                line = line.replace('PHARMACY-', 'PHARMACY:')
            
            # Ensure key-value separation is consistent
            # Replace missing colons after known keys
            line = re.sub(r'\b(Name|ID|No|Date|Status|Type|Sex|Age|Class)\s+(?!:)', r'\1: ', line)
            
            # Handle multiple key-value pairs in same brackets
            if ' & ' in line:
                line = line.replace(' & ', '\n')
            
            # Handle Yes/No values that might have been converted to true/false
            line = re.sub(r'\b(true|false)\b', lambda m: m.group(0).lower(), line, flags=re.IGNORECASE)
        
        formatted_lines.append(line)
    
    return '\n'.join(formatted_lines)

def read_markdown_file(file_path):
    """Read the content of a markdown file."""
    with open(file_path, 'r', encoding='utf-8') as file:
        return file.read()

def extract_ocr_text(markdown_content):
    """Extract the OCR text from markdown content."""
    # Find the text between the first ``` and the last ```
    start = markdown_content.find('```\n') + 4
    end = markdown_content.rfind('\n```')
    if start > 3 and end > 0:
        return markdown_content[start:end]
    return ""

def preprocess_ocr_text(ocr_text: str) -> str:
    """Apply all preprocessing steps to OCR text."""
    text = clean_ocr_text(ocr_text)
    text = process_checkboxes(text)
    text = format_key_values(text)
    return text

def extract_simple_services(lines: List[str]) -> List[Dict[str, Any]]:
    """Parse the "headers first, cells below" table layout that the
    markdown OCR output uses when the dedicated table parser fails.

    Returns a list of dictionaries, each representing one medical service.
    """

    # ---------- Helpers ----------
    def clean_token(token: str) -> str:
        """Normalise a raw markdown line for easier matching."""
        token = token.strip()
        # Remove leading/trailing square brackets and any surrounding quotes
        token = token.lstrip("[").rstrip("]")
        token = re.sub(r"^[\[{](.*)[\]}]$", r"\1", token)  # remove any leftover braces
        token = re.sub(r"['\"]", "", token)  # drop quotes
        token = re.sub(r",", " ", token)      # commas → spaces
        token = re.sub(r"\s+", " ", token)    # collapse whitespace
        return token.lower().strip()

    # Map various OCR header spellings to canonical field names
    header_aliases = {
        "(code) service": "codeService",
        "(code)service": "codeService",
        "code service": "codeService",
        "codeservice": "codeService",
        "type": "type",
        "req.qty": "reqQty",
        "req qty": "reqQty",
        "req.quantity": "reqQty",
        "req.cost": "reqCost",
        "req cost": "reqCost",
        "gross amount": "grossAmount",
        "gross": "grossAmount",
        "app.qty": "appQty",
        "app qty": "appQty",
        "approved qty": "appQty",
        "approved quantity": "appQty",
        "app.cost": "appCost",
        "app cost": "appCost",
        "approved cost": "appCost",
        "app.gross": "appGross",
        "app gross": "appGross",
        "note": "note",
    }

    # ---------- 1) Identify header block ----------
    headers: List[str] = []
    header_end_idx: Optional[int] = None

    for idx, raw_line in enumerate(lines):
        cleaned = clean_token(raw_line)
        if not cleaned:
            continue

        if cleaned in header_aliases or ('(code)' in cleaned and 'service' in cleaned):
            canonical = header_aliases.get(cleaned, cleaned)
            if canonical not in headers:
                headers.append(canonical)

            # Terminate header block once we reach the final header ('note')
            if canonical == 'note':
                header_end_idx = idx + 1
                break
        else:
            # In case OCR merged header text, attempt partial matches
            for alias, field in header_aliases.items():
                if alias in cleaned and field not in headers:
                    headers.append(field)
                    if field == 'note':
                        header_end_idx = idx + 1
                        break
            if header_end_idx:
                break

    if not headers:
        return []  # Could not determine headers

    if header_end_idx is None:
        header_end_idx = len(lines)

    # ---------- 2) Gather cell values ----------
    cells: List[str] = []
    for raw_line in lines[header_end_idx:]:
        cleaned = clean_token(raw_line)
        if cleaned:
            cells.append(cleaned)

    if not cells:
        return []

    # ---------- 3) Chunk into rows ----------
    row_size = len(headers)
    services: List[Dict[str, Any]] = []

    for start in range(0, len(cells), row_size):
        chunk = cells[start:start + row_size]
        if len(chunk) < row_size:
            break  # ignore incomplete final row

        row: Dict[str, Any] = {}
        for header_field, cell_value in zip(headers, chunk):
            field_name = header_field  # already canonical

            if field_name == 'codeService':
                # Expected pattern: (CODE) Description of service
                m = re.match(r"\(([^)]+)\)\s*(.*)", cell_value, flags=re.IGNORECASE)
                if m:
                    row['code'] = m.group(1).strip()
                    desc = m.group(2).strip()
                    if desc:
                        row['description'] = desc
                else:
                    row['description'] = cell_value
                continue

            # Convert numbers where sensible
            if field_name in {
                'reqQty', 'reqCost', 'grossAmount',
                'appQty', 'appCost', 'appGross'
            }:
                try:
                    row[field_name] = float(cell_value)
                except ValueError:
                    row[field_name] = None
            else:
                row[field_name] = cell_value

        services.append(row)

    return services

def find_payer_info(lines: List[str]) -> str:
    """Extract payer information from the form."""
    payer_info = []
    
    # Look for payer information patterns
    for line in lines:
        line_lower = line.lower()
        
        # Explicit payer marker
        if "payer:" in line_lower:
            parts = re.split(r'payer\s*:', line, flags=re.IGNORECASE)
            if len(parts) > 1:
                payer_info.append(parts[1].strip())
        
        # Common payer message patterns
        elif any(pattern in line_lower for pattern in [
            "please note", "amount of", "requested services", "do not require", 
            "prior approval", "policy's terms", "kindly provide", "necessary medical services"
        ]):
            payer_info.append(line)
    
    if payer_info:
        return " ".join(payer_info)
    
    return ""

def find_service_table_section(lines: List[str]) -> List[str]:
    """Locate and return the slice of *lines* that corresponds to the service table.
    The form uses only **one** layout (formerly called *format1*), so we simply
    search for the first occurrence of an indicative header such as
    "(code) service", then gather a reasonable window of lines afterward.
    """

    start_idx: Optional[int] = None
    end_idx: Optional[int] = None

    for i, line in enumerate(lines):
        line_lower = line.lower()

        # The service table starts around the header "(code) service" or any
        # line that contains a parenthesised code pattern.
        if '(code)' in line_lower and 'service' in line_lower:
            start_idx = i
            break

        # Fallback: detect the first line with the code pattern (90911-00-00)
        if start_idx is None and re.search(r'\(\d+[^)]*-\d+[^)]*\)', line_lower):
            start_idx = i - 1 if i > 0 else 0  # include possible header line just before
            break

    if start_idx is None:
        # Could not detect a dedicated table; return the whole document so that
        # downstream logic can attempt best-effort parsing.
        return lines

    # Heuristic: table rarely exceeds 30 lines; stop earlier when we hit
    # obvious unrelated sections.
    for j in range(start_idx, len(lines)):
        line_lower = lines[j].lower()
        if any(marker in line_lower for marker in [
            'no data to be shown',
            'in case management',
            'i hereby',
            'medication',
            'completed/coded',
            'providers approval']):
            end_idx = j
            break

    if end_idx is None:
        end_idx = min(start_idx + 30, len(lines))

    # Include a few lines before the detected start to capture headers.
    safe_start = max(0, start_idx - 5)
    return lines[safe_start:end_idx]

def extract_service_format(table_lines: List[str]) -> List[Dict[str, Any]]:
    """Extract service information from Format 1 table with improved robustness."""
    services = []
    
    # This will store all content that might be part of a service
    all_lines = []
    for line in table_lines:
        if line.strip():
            all_lines.append(line.strip())
    
    # First, identify primary service codes with format: (90911-00-00)
    service_sections = []
    current_section = []
    
    for i, line in enumerate(all_lines):
        # Check if line contains a primary service code
        if re.search(r'\((\d+[^)]*-\d+[^)]*)\)', line):
            # If we already have a section, save it
            if current_section:
                service_sections.append(current_section)
                current_section = []
            
            # Start a new section
            current_section.append(line)
        elif current_section:
            # Add to current section
            current_section.append(line)
    
    # Add the last section if it exists
    if current_section:
        service_sections.append(current_section)
    
    # Process each service section
    for section in service_sections:
        # Initialize service data
        service = {}
        
        # Extract primary code
        for line in section:
            primary_match = re.search(r'\((\d+[^)]*-\d+[^)]*)\)', line)
            if primary_match:
                service['code'] = primary_match.group(1)
                
                # Extract description after code
                desc_part = line[primary_match.end():].strip()
                if desc_part:
                    service['description'] = desc_part
                break
        
        # If no primary code found, skip this section
        if 'code' not in service:
            continue
        
        # Extract additional codes
        additional_codes = []
        for line in section:
            # Look for additional codes but not the primary code
            if 'code' in service and service['code'] not in line:
                add_match = re.search(r'\((\d+)\)', line)
                if add_match:
                    additional_codes.append(add_match.group(1))
                    
                    # If the additional code is in a line with text, add to description
                    if 'description' not in service:
                        text_without_code = re.sub(r'\(\d+\)', '', line).strip()
                        if text_without_code:
                            service['description'] = text_without_code
                    else:
                        text_without_code = re.sub(r'\(\d+\)', '', line).strip()
                        if text_without_code and text_without_code not in service['description']:
                            service['description'] += " " + text_without_code
        
        if additional_codes:
            service['additionalCodes'] = additional_codes
        
        # Look for service type
        for line in section:
            if line.strip() in ["Imaging", "Lab", "Services", "Consultation"]:
                service['type'] = line.strip()
                break
        
        # Look for numeric values
        numeric_values = []
        for line in section:
            if re.match(r'^\d+\.?\d*$', line):
                numeric_values.append(float(line))
        
        # Assign numeric values to fields
        field_names = ["reqQty", "reqCost", "grossAmount", "appQty", "appCost", "appGross", "note"]
        for idx, value in enumerate(numeric_values):
            if idx < len(field_names):
                service[field_names[idx]] = value
        
        # Look for status
        for line in section:
            if line.strip() in ["Not Required", "Approved", "Partial"]:
                service['status'] = line.strip()
                break
        
        # Clean up description if it exists
        if 'description' in service:
            service['description'] = clean_service_description(service['description'])
        
        # Add the service to our results
        if service:
            services.append(service)
    
    return services

def clean_service_description(raw_description: str) -> str:
    """Clean up service description by removing unwanted parts."""
    # Unwanted parts that might appear in descriptions
    unwanted_sections = [
        # Service Provider & Staff sections
        "services Providers", "Providers Approval", "Approval/Coding", "Staff must", "review/code", 
        "completethe following", "Completed/Coded", "Signature", "Date", "Medication",
        # Form elements that aren't part of description
        "Type Req", "Req. Qty", "Req. Cost", "Gross amount", "App. Qty", "App. Cost", "App. Gross", "Note",
        # Additional unwanted parts that might be present
        "Providers", "Staff", "Generic", "Signature", "Coded By"
    ]
    
    # Find the earliest cutoff point
    earliest_cutoff = len(raw_description)
    for section in unwanted_sections:
        pos = raw_description.find(section)
        if pos != -1 and pos < earliest_cutoff:
            earliest_cutoff = pos
    
    # Cut off at the earliest unwanted section
    clean_description = raw_description[:earliest_cutoff].strip()
    
    # Clean up extra spaces
    clean_description = re.sub(r'\s+', ' ', clean_description).strip()
    return clean_description

def convert_to_json(ocr_text: str, file_name: str) -> Dict[str, Any]:
    """Convert OCR text to JSON using GPT-4 and validate with Pydantic models."""
    # Preprocess the OCR text
    processed_text = preprocess_ocr_text(ocr_text)
    
    # Construct the prompt with additional instructions
    additional_instructions = """
IMPORTANT INSTRUCTIONS:
1. Only convert parentheses to true/false for checkbox fields (single, married, newVisit, etc.). For other fields like signature, keep the original text.
2. PRESERVE ALL ARABIC TEXT EXACTLY AS IT APPEARS - DO NOT TRANSLATE:
   - Policy Holder field may contain Arabic text
   - Names may contain Arabic text
   - Any other field may contain Arabic text
3. For fields containing Arabic text:
   - Keep the exact Arabic characters
   - Do not transliterate to English
   - do not modify the text in any way
4. If a field contains mixed Arabic and English, preserve both exactly as they appear
5. In the services tables: 
   - the markdown file will contain only ONE of TWO different formats of tables, either servicesTable1 formate or servicesTable2 format:
     - If the first header is [code] then this is servicesTable2 format which has the following headers: `code, nonStandardCode, description, type, totalQuantity, cost, approvedQuantity, approvedCost, status`. 
     - If the first header is [(code) service] then this is servicesTable1 format which has the following headers: `codeService, type, reqQty, reqCost, grossAmount, appQty, appCost, appGross, note`.
     - in the markdown file all the headers are listed first followed by all the values consecutively.
     - Sometimes a single header may have 2 words above each other in this case you should combine them in a single header, example:
       - [Req.] [Qty] = ReqQty
       - [App.] [Qty] = AppQty
       - [Req.] [Cost] = ReqCost
       - [App.] [Cost] = AppCost
       - [Gross] [Amount] = GrossAmount
       - [App.] [Gross] = AppGross
     - the codeService is the first header in the `services table 2`, the value incudes the code between parenthesis `(code)` followed by `description of the service`, 
       there may be several `(code) description` pairs in this value of codeService extending on multiple lines. If the number of lines exceed 2, the remaining lines may be found after all the other values are listed. 
       So if the value of codeService ends with an incomplete word or part of a word look for the logical completion of the word in line 3 after all the other values are listed.
6. In CertifcationInfo, nameAndReleationship field refers to the name of the patient or guardian. Here is an example:
{
  "physicianName": "is the first item in this section and may or may not be precended by the word Dr. or Dr",
  "physicianSignature": false,
  "physicianSignatureDate": "2024-01-01",
  "nameAndRelationship": "Name of patient and name of guardian (if one exists)",
  "patientSignature": false,
  "patientSignatureDate": "2024-01-01"
}
"""
    full_prompt = MAIN_PROMPT + additional_instructions + "\n\nHere's the OCR text:\n\n" + processed_text
    
    # Make the API call
    response = client.chat.completions.create(
        model="gpt-4o-2024-11-20",
        messages=[
            {
                "role": "system", 
                "content": """You are a helpful assistant that converts OCR text to structured JSON. 
Important guidelines:
- Pay special attention to field types - only use boolean true/false for checkbox fields
- Use strings for text fields like signatures and names
- PRESERVE ALL ARABIC TEXT EXACTLY AS IT APPEARS - DO NOT TRANSLATE
- Keep Arabic text in its original form, especially in fields like Policy Holder
- Do not transliterate or modify any Arabic text
- For mixed Arabic/English text, preserve both languages exactly as they appear
"""
            },
            {"role": "user", "content": full_prompt}
        ],
        temperature=0,
        max_tokens=4000
    )
    
    # Extract and parse the JSON response
    json_str = response.choices[0].message.content
    # Find the JSON part (between first { and last })
    start = json_str.find('{')
    end = json_str.rfind('}') + 1
    if start >= 0 and end > 0:
        json_str = json_str[start:end]
    
    try:
        # Parse the raw JSON first
        raw_json = json.loads(json_str)
        
        # Create a StructuredOCR object with the parsed data
        structured_data = StructuredOCR(
            file_name=file_name,
            topics=["medical_form"],
            languages=[Language.ENGLISH, Language.ARABIC],
            ocr_contents=MedicalFormContent(**raw_json),
            document_type="UCAF Medical Form",
            confidence_score=0.95  # Example score
        )
        
        # Extract payer information using specialized function
        lines = ocr_text.split('\n')
        payer_info = find_payer_info(lines)
        if payer_info and "ocr_contents" in structured_data.model_dump(exclude_none=True):
            # Update the payer information in the structured data
            json_data = structured_data.model_dump(exclude_none=True)
            # Check if payerInfo exists in the JSON, if not create it
            if "payerInfo" not in json_data["ocr_contents"] or not json_data["ocr_contents"]["payerInfo"]:
                json_data["ocr_contents"]["payerInfo"] = {"comments": payer_info}
            else:
                # Append extracted payer info to existing comments
                existing_comments = json_data["ocr_contents"]["payerInfo"].get("comments", "")
                if existing_comments:
                    json_data["ocr_contents"]["payerInfo"]["comments"] = f"{existing_comments} {payer_info}"
                else:
                    json_data["ocr_contents"]["payerInfo"]["comments"] = payer_info
            
            # Update the structured data with the enhanced payer info
            structured_data = StructuredOCR(**json_data)
        
        # ---------- Service extraction ----------
        services = None
        extraction_method = "none"

        # 1) Try the vertical-table parser first (matches actual form layout)
        table_lines = find_service_table_section(lines)
        if table_lines:
            services = extract_simple_services(table_lines)
            if services:
                extraction_method = "vertical"

        # 2) Fallback to legacy parser if vertical parser found nothing
        if (not services) and table_lines:
            services = extract_service_format(table_lines)
            if services:
                extraction_method = "format1"
        
        print(f"DEBUG: Using extraction method: {extraction_method}")
        print(f"DEBUG: Final extracted services: {services}")
        
        if services and "ocr_contents" in structured_data.model_dump(exclude_none=True):
            # Replace in the structured data
            json_data = structured_data.model_dump(exclude_none=True)
            
            # Properly format the services with all fields
            formatted_services = []
            for service in services:
                print(f"DEBUG: Processing service: {service}")
                
                formatted_service = {
                    "code": service.get("code", ""),
                    "description": service.get("description", ""),
                    "additionalCodes": service.get("additionalCodes", [])
                }
                
                # Clean up description with general pattern matching if needed
                if "description" in formatted_service:
                    desc = formatted_service["description"]
                    # Make sure it's a string
                    if not isinstance(desc, str):
                        desc = str(desc)
                    
                    # Replace complex array notation with simple text
                    if '[' in desc and ']' in desc:
                        # Extract all content from inside list/array notations
                        content_matches = re.findall(r'\[(.*?)\]', desc)
                        extracted_content = []
                        for match in content_matches:
                            # Extract words from inside quotes
                            words = re.findall(r'\'([^\']+)\'', match)
                            if words:
                                extracted_content.extend(words)
                        
                        # Join all extracted words and replace original description
                        if extracted_content:
                            desc = ' '.join(extracted_content)
                    
                    # Apply general pattern matching
                    desc = re.sub(r'(\w+)\s+um\b', r'\1um', desc)
                    desc = re.sub(r'(\w+)\s+er\b', r'\1er', desc)
                    desc = re.sub(r'(\w+)\s+ing\b', r'\1ing', desc)
                    desc = re.sub(r'(\w+)\s+ed\b', r'\1ed', desc)
                    desc = re.sub(r'(\w+)\s+al\b', r'\1al', desc)
                    desc = re.sub(r'(\w+)\s+sis\b', r'\1sis', desc)
                    desc = re.sub(r'(\w+)\s+tion\b', r'\1tion', desc)
                    desc = re.sub(r'(\w+)\s+phy\b', r'\1phy', desc)
                    desc = re.sub(r'(\w+)\s+gram\b', r'\1gram', desc)
                    
                    # Remove any remaining brackets, quotes, commas
                    desc = re.sub(r'[\[\]\'\",]', '', desc)
                    desc = re.sub(r'\s+', ' ', desc)  # Normalize whitespace
                    desc = re.sub(r'```.*$', '', desc) # Remove markdown
                    desc = re.sub(r'\s+Date.*$', '', desc)
                    desc = re.sub(r'\s+---.*$', '', desc)
                    
                    formatted_service["description"] = desc.strip()
                
                # Map numeric fields
                if "reqQty" in service:
                    formatted_service["requestedQuantity"] = service["reqQty"]
                    print(f"DEBUG: Adding requestedQuantity: {service['reqQty']}")
                if "reqCost" in service:
                    formatted_service["requestedCost"] = service["reqCost"]
                    print(f"DEBUG: Adding requestedCost: {service['reqCost']}")
                if "grossAmount" in service:
                    formatted_service["grossAmount"] = service["grossAmount"]
                    print(f"DEBUG: Adding grossAmount: {service['grossAmount']}")
                if "appQty" in service:
                    formatted_service["approvedQuantity"] = service["appQty"]
                    print(f"DEBUG: Adding approvedQuantity: {service['appQty']}")
                if "appCost" in service:
                    formatted_service["approvedCost"] = service["appCost"]
                    print(f"DEBUG: Adding approvedCost: {service['appCost']}")
                if "appGross" in service:
                    formatted_service["approvedGross"] = service["appGross"]
                    print(f"DEBUG: Adding approvedGross: {service['appGross']}")
                
                # Add type if available
                if "type" in service:
                    formatted_service["serviceType"] = service["type"]
                    print(f"DEBUG: Adding serviceType: {service['type']}")
                
                # Add status if available
                if "status" in service:
                    formatted_service["status"] = service["status"]
                    print(f"DEBUG: Adding status: {service['status']}")
                
                # Add note if available
                if "note" in service:
                    formatted_service["note"] = service["note"]
                    print(f"DEBUG: Adding note: {service['note']}")
                
                formatted_services.append(formatted_service)
                print(f"DEBUG: Final formatted service: {formatted_service}")
            
            json_data["ocr_contents"]["suggestedServices"] = formatted_services
            print(f"DEBUG: Final suggestedServices: {json_data['ocr_contents']['suggestedServices']}")
            return json_data
        
        # Convert to dict for JSON serialization if no service extraction was done
        return structured_data.model_dump(exclude_none=True)
    except Exception as e:
        print(f"Error processing JSON: {e}")
        return None

def main():
    # Input and output paths
    input_md = "outputs/az_results_1.md"
    output_json = "outputs/az_results_1.json"
    
    # Read the markdown file
    markdown_content = read_markdown_file(input_md)
    
    # Extract OCR text
    ocr_text = extract_ocr_text(markdown_content)
    
    # Convert to JSON using the file name as reference
    json_data = convert_to_json(ocr_text, os.path.basename(input_md))
    
    if json_data:
        # Write the JSON output
        with open(output_json, 'w', encoding='utf-8') as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)
        print(f"Successfully converted {input_md} to {output_json}")
    else:
        print("Failed to convert OCR text to JSON")

if __name__ == "__main__":
    main() 