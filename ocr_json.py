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

# ------------------------ NEW PAYER EXTRACTION FUNCTION ------------------------

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

# ------------------------ NEW SERVICE EXTRACTION FUNCTIONS ------------------------

def find_service_table_section(lines: List[str]) -> Tuple[List[str], str]:
    """
    Find the section containing the service table and determine format.
    Improved to handle poorly arranged Format 1 data.
    """
    start_idx = None
    end_idx = None
    format_type = 'unknown'  # Start with unknown format
    format1_score = 0
    format2_score = 0
    
    # First scan: Look for strong format indicators and calculate format scores
    for i, line in enumerate(lines):
        line_lower = line.lower()
        
        # Format 1 indicators (with parenthesized codes)
        if '(code)' in line_lower and 'service' in line_lower:
            format1_score += 5
            if start_idx is None:
                start_idx = i
        
        # Look for parenthesized codes like (90911-00-00)
        elif re.search(r'\(\d+[^)]*-\d+[^)]*\)', line_lower):
            format1_score += 3
            if start_idx is None:
                start_idx = i
        
        # Look for "Req." and "App." markers - strong Format 1 indicators
        elif 'req.' in line_lower or 'app.' in line_lower:
            format1_score += 2
            
        # Look for "Gross amount" - Format 1 indicator
        elif 'gross' in line_lower and 'amount' in line_lower:
            format1_score += 2
        
        # Format 2 indicators
        if line_lower == 'code' or line_lower.startswith('code '):
            format2_score += 3
            if start_idx is None:
                start_idx = i
                
        # Look for "non standard code" - strong Format 2 indicator
        elif 'non standard code' in line_lower:
            format2_score += 4
            
        # Look for "description/service" - Format 2 indicator
        elif 'description/service' in line_lower:
            format2_score += 3
            
        # Look for "approved quantity" and "approved cost" - Format 2 indicators
        elif ('approved quantity' in line_lower or 'approved cost' in line_lower):
            format2_score += 2
            
        # Look for end of table markers
        if start_idx is not None and any(pattern in line_lower for pattern in 
                                       ['no data to be shown', 'in case management', 'i hereby']):
            end_idx = i
            break
    
    # Determine format based on scores
    if format1_score > format2_score:
        format_type = 'format1'
    elif format2_score > format1_score:
        format_type = 'format2'
    else:
        # If tied, check for additional signals
        for i, line in enumerate(lines):
            if re.search(r'\((\d+[^)]*-\d+[^)]*)\)', line):
                format_type = 'format1'
                break
        if format_type == 'unknown':
            format_type = 'format1'  # Default to format1 if still uncertain
    
    # If we found a start but no end, estimate a reasonable range
    if start_idx is not None:
        if end_idx is None:
            end_idx = min(start_idx + 30, len(lines))
        
        # For more reliability, include a few lines before the detected start
        # in case some headers were missed
        safe_start = max(0, start_idx - 5)
        
        return lines[safe_start:end_idx], format_type
    
    # If we couldn't determine a section, return a reasonable portion of the document
    # and let the extraction functions handle it
    if len(lines) > 10:
        middle = len(lines) // 2
        section_start = max(0, middle - 15)
        section_end = min(len(lines), middle + 15)
        return lines[section_start:section_end], 'format1'  # Default to format1
    
    return lines, 'format1'  # Default to full document and format1

def extract_service_format1(table_lines: List[str]) -> List[Dict[str, Any]]:
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

def extract_service_format2(table_lines: List[str]) -> List[Dict[str, Any]]:
    """Extract service information from Format 2 table: code, non-standard code pattern."""
    services = []
    headers = []
    
    # Find the header row and data rows
    header_found = False
    data_start = 0
    
    # First, identify header lines
    for i, line in enumerate(table_lines):
        line_lower = line.lower()
        
        if not header_found:
            if 'code' in line_lower or 'description' in line_lower or 'type' in line_lower:
                headers.append(line)
                if 'status' in line_lower or 'approved cost' in line_lower:
                    header_found = True
                    data_start = i + 1
        else:
            # We've found all headers, now look for data
            break
    
    # If headers weren't clearly found, estimate headers from known patterns
    if not header_found and len(headers) < 3:
        headers = ['Code', 'Non Standard Code', 'Description/Service', 'Type', 
                  'Total Quantity', 'Cost', 'Approved Quantity', 'Approved Cost', 'Status']
        data_start = 0
        
        # Try to find where data starts after these headings
        for i, line in enumerate(table_lines):
            if re.match(r'^\d+[^a-zA-Z]*$', line.strip()):
                data_start = i
                break
    
    # Map headers to standard field names - includes both Format 1 and Format 2 mappings
    header_mapping = {
        # Format 2 mappings
        'code': 'code',
        'non standard code': 'nonStandardCode',
        'description/service': 'description',
        'type': 'type',
        'total quantity': 'reqQty',
        'cost': 'reqCost',
        'approved quantity': 'appQty',
        'approved cost': 'appCost',
        'status': 'status',
        
        # Format 1 mappings (in case they appear in Format 2)
        '(code) service': 'codeService',
        'gross amount': 'grossAmount',
        'app. gross': 'appGross',
        'app.gross': 'appGross',
        'note': 'note'
    }
    
    # Process data rows
    current_row = {}
    field_index = 0
    
    # Go through all lines after headers
    for i in range(data_start, len(table_lines)):
        line = table_lines[i].strip()
        
        # Skip empty lines
        if not line:
            continue
        
        # Check if this is a code line (indicates start of new service)
        if re.match(r'^\d+[^a-zA-Z]*$', line) and (field_index == 0 or field_index >= len(headers)):
            # Save previous service if exists
            if current_row and 'code' in current_row:
                services.append(current_row)
                current_row = {}
            
            # Start new service
            current_row['code'] = line
            field_index = 1
        
        # If we have a partial row, continue filling it
        elif current_row:
            # Map field by position
            if field_index < len(headers):
                header = headers[field_index].lower()
                field_name = None
                
                # Find matching field name
                for key, value in header_mapping.items():
                    if key in header.lower():
                        field_name = value
                        break
                
                if field_name:
                    # Handle numeric fields
                    if field_name in ['reqQty', 'reqCost', 'appQty', 'appCost', 'grossAmount', 'appGross', 'note'] and re.match(r'^\d+\.?\d*$', line):
                        current_row[field_name] = float(line)
                    else:
                        current_row[field_name] = line
                
                field_index += 1
            
            # If we've filled all fields, reset for next row
            if field_index >= len(headers):
                field_index = 0
    
    # Add the last service
    if current_row and 'code' in current_row:
        services.append(current_row)
    
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
   - Do not modify the text in any way
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
6. In CertifcationInfo, the physician name is always after nameAndReleationship field. Here is an example:
{
  "physician": "Dr. Jane John Doe or Jane John Doe",
  "signature": false,
  "stamp": false,
  "date": "2024-01-01",
  "nameAndRelationship": "Jane John Doe",
  "o": false,
  "signature": false,
  "date": "2024-01-01"
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
        
        # Extract services using the specialized extractor
        lines = ocr_text.split('\n')

        # Find the service table section and determine format
        table_lines, format_type = find_service_table_section(lines)

        if table_lines:
            # Extract services based on format
            services = None
            if format_type == 'format1':
                services = extract_service_format1(table_lines)
            else:  # format2
                services = extract_service_format2(table_lines)
            
            # If no services found with the detected format, try the other format as backup
            if not services and format_type == 'format1':
                services = extract_service_format2(table_lines)
            elif not services and format_type == 'format2':
                services = extract_service_format1(table_lines)
            
            if services and "ocr_contents" in structured_data.model_dump(exclude_none=True):
                # Add the services to the OCR contents
                formatted_services = []
                
                for service in services:
                    formatted_service = {
                        "code": service.get("code", ""),
                        "description": service.get("description", "")
                    }
                    
                    # Add additional codes if available
                    if "additionalCodes" in service:
                        formatted_service["additionalCodes"] = service["additionalCodes"]
                    
                    # Map other fields to the expected structure - FIXED WITH COMPLETE MAPPINGS
                    field_mapping = {
                        # Format 2 specific fields
                        "code": "code",
                        "nonStandardCode": "nonStandardCode",
                        "status": "status",
                        
                        # Common fields with different names
                        "type": "serviceType",
                        "reqQty": "totalQuantity",
                        "reqCost": "cost",                   
                        "appQty": "approvedQuantity",
                        "appCost": "approvedCost",
                                                
                        # Format 1 specific fields
                        "codeService": "codeService",
                        "grossAmount": "grossAmount",
                        "appGross": "approvedGross",
                        "note": "note" 
                    }
                    
                    for src_key, dest_key in field_mapping.items():
                        if src_key in service:
                            formatted_service[dest_key] = service[src_key]
                    
                    formatted_services.append(formatted_service)
                
                # Replace in the structured data
                json_data = structured_data.model_dump(exclude_none=True)
                json_data["ocr_contents"]["suggestedServices"] = formatted_services
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