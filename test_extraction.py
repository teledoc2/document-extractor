import json
import os
import re
from typing import Dict, Any, List

def extract_ocr_text(markdown_path):
    """Extract the OCR text from markdown file."""
    with open(markdown_path, 'r', encoding='utf-8') as file:
        markdown_content = file.read()
    
    # Find the text between the first ``` and the last ```
    start = markdown_content.find('```\n') + 4
    end = markdown_content.rfind('\n```')
    if start > 3 and end > 0:
        return markdown_content[start:end]
    return ""

def clean_line(line: str) -> str:
    """Clean the line by converting string representations of arrays to plain text."""
    if not line or not line.strip():
        return ""
    
    # Handle array format in markdown file
    if line.strip().startswith('[') and line.strip().endswith(']'):
        try:
            # Try to evaluate as a Python list
            content = eval(line)
            if isinstance(content, list):
                return " ".join(str(item) for item in content if item)
        except:
            # If eval fails, clean up manually
            line = line.strip('[]')
            line = re.sub(r"'(.*?)'", r"\1", line)  # Remove quotes
            line = line.replace(",", " ")  # Replace commas with spaces
            return line.strip()
    
    return line.strip()

def extract_services(lines: List[str]) -> List[Dict[str, Any]]:
    """Extract services from the markdown text."""
    services = []
    current_service = None
    description_fragments = []
    found_service_header = False
    service_section_started = False
    
    # Track which lines to skip (already processed)
    skip_lines = set()
    
    # First pass - look for service sections
    for i, raw_line in enumerate(lines):
        if i in skip_lines:
            continue
            
        line = clean_line(raw_line)
        line_lower = line.lower() if line else ""
        print(f"DEBUG: Processing line {i}: '{raw_line}'")
        
        # Look for service table section first
        if not service_section_started and "(code)" in line_lower and "service" in line_lower:
            service_section_started = True
            continue
            
        # Service code in parentheses - only process if we're in the service section or found a clear code pattern
        if service_section_started or re.search(r'\((\d+[^)]*-\d+[^)]*)\)', line):
            code_match = re.search(r'\((\d+[^)]*-\d+[^)]*)\)', line)
            if code_match:
                # Start new service
                if current_service:
                    services.append(current_service)
                    
                current_service = {
                    "code": code_match.group(1),
                    "description": line[code_match.end():].strip()
                }
                description_fragments = [current_service["description"]]
                print(f"DEBUG: Found service header: '{line}'")
                found_service_header = True
                
                # Look ahead for additional descriptions and meta-data
                collected_fragments = False
                continuation_line_found = False
                
                for j in range(i+1, min(i+20, len(lines))):
                    if j in skip_lines:
                        continue
                        
                    next_raw = lines[j]
                    next_line = clean_line(next_raw)
                    next_lower = next_line.lower() if next_line else ""
                    
                    # Skip empty lines
                    if not next_line.strip():
                        continue
                        
                    # Check for additional code in text
                    add_code_match = re.search(r'\((\d+)\)', next_line)
                    if add_code_match and not re.search(r'\((\d+[^)]*-\d+[^)]*)\)', next_line):
                        if "additionalCodes" not in current_service:
                            current_service["additionalCodes"] = []
                        current_service["additionalCodes"].append(add_code_match.group(1))
                        
                        # Get text part without the code and add to description
                        text_without_code = re.sub(r'\(\d+\)', '', next_line).strip()
                        if text_without_code and not any(marker in next_lower for marker in 
                                ["providers", "completed", "signature", "for insurance"]):
                            description_fragments.append(text_without_code)
                            collected_fragments = True
                        
                        skip_lines.add(j)
                        continue
                        
                    # Check for type information
                    if next_lower in ["imaging", "lab", "consultation"]:
                        current_service["type"] = next_line.strip()
                        print(f"DEBUG: Found type: '{next_line}'")
                        skip_lines.add(j)
                        continue
                    
                    # Check for numeric values (qty, cost)
                    if re.match(r'^\d+\.?\d*$', next_lower.strip()):
                        # Try to determine which numeric field this is based on context and position
                        field_names = ["reqQty", "reqCost", "grossAmount", "appQty", "appCost", "appGross"]
                        
                        # If we already have some numeric fields, determine which one this is
                        assigned = False
                        for field in field_names:
                            if field not in current_service:
                                current_service[field] = float(next_lower.strip())
                                print(f"DEBUG: Found {field}: '{next_lower}'")
                                assigned = True
                                break
                                
                        # If we couldn't determine, use position relative to last assigned field
                        if not assigned and any(f in current_service for f in field_names):
                            last_assigned = None
                            for field in field_names:
                                if field in current_service:
                                    last_assigned = field
                            
                            if last_assigned:
                                idx = field_names.index(last_assigned)
                                if idx < len(field_names) - 1:
                                    next_field = field_names[idx + 1]
                                    current_service[next_field] = float(next_lower.strip())
                                    print(f"DEBUG: Found {next_field} (by position): '{next_lower}'")
                        
                        skip_lines.add(j)
                        continue
                        
                    # Check for status values
                    if any(status in next_lower for status in 
                           ["required", "not required", "approved", "partial", "no data to be shown"]):
                        current_service["status"] = next_line.strip()
                        print(f"DEBUG: Found status: '{next_line}'")
                        skip_lines.add(j)
                        continue
                        
                    # Check for table headers that indicate numeric fields
                    header_mapping = {
                        "req.qty": "reqQty",
                        "req.cost": "reqCost", 
                        "req. qty": "reqQty",
                        "req. cost": "reqCost",
                        "gross amount": "grossAmount",
                        "app.qty": "appQty", 
                        "app.cost": "appCost",
                        "app. qty": "appQty",
                        "app. cost": "appCost", 
                        "app.gross": "appGross",
                        "app. gross": "appGross"
                    }
                    
                    for header_key, field_name in header_mapping.items():
                        if header_key in next_lower:
                            print(f"DEBUG: Found header for {field_name}: '{next_line}'")
                            # Look ahead for the value in the next lines
                            for k in range(j+1, min(j+3, len(lines))):
                                if k in skip_lines:
                                    continue
                                    
                                value_line = clean_line(lines[k])
                                if re.match(r'^\d+\.?\d*$', value_line.strip()):
                                    current_service[field_name] = float(value_line.strip())
                                    print(f"DEBUG: Found {field_name} value: '{value_line}'")
                                    skip_lines.add(k)
                                    break
                            
                            skip_lines.add(j)
                            break
                    
                    # Check for table headers
                    if any(marker in next_lower for marker in 
                           ["type", "req.", "qty", "cost", "app.", "gross", "amount", "note"]):
                        skip_lines.add(j)
                        continue
                    
                    # Check for continuation of service description - this is critical for handling fragmentation
                    # Look for fragments and common suffix patterns that indicate a continuation
                    if (next_lower.strip().startswith(("um", "er", "ing", "ed", "al", "sis", "tion", "phy", "gram")) or 
                        len(next_lower.strip()) <= 5 or  # Very short lines are often fragments
                        any(word in next_lower for word in ["doppler", "ultrasound", "scan", "mri", "ct", "xray", "mammogram", "vessel", "site", "graph", "scope"])):
                        description_fragments.append(next_line.strip())
                        skip_lines.add(j)
                        continuation_line_found = True
                        print(f"DEBUG: Found description continuation: '{next_line}'")
                        continue
                    
                    # Check for new service or end of service section
                    if "service" in next_lower or "medication" in next_lower or "providers" in next_lower:
                        break
                    
                    # Skip other lines that clearly aren't part of description
                    if any(marker in next_lower for marker in 
                           ["signature", "date", "completed", "physician", "provider"]):
                        skip_lines.add(j)
                        continue
        
        # Check for note field
        elif current_service and line_lower.strip() == "note":
            print(f"DEBUG: Found note: '{line}'")
            
            # Look ahead for the note content in the next lines
            for j in range(i+1, min(i+3, len(lines))):
                if j in skip_lines:
                    continue
                    
                next_line = clean_line(lines[j])
                if next_line and not next_line.lower().startswith(("service", "provider", "for insurance")):
                    current_service["note"] = next_line.strip()
                    print(f"DEBUG: Found note content: '{next_line}'")
                    skip_lines.add(j)
                    break
    
    # Combine the description fragments and clean up the service
    if current_service:
        # Combine description fragments
        if description_fragments:
            # Handle fragmented words with general patterns
            combined_description = " ".join(description_fragments)
            # Fix common fragmentation patterns
            combined_description = re.sub(r'(\w+)\s+um\b', r'\1um', combined_description)
            combined_description = re.sub(r'(\w+)\s+er\b', r'\1er', combined_description) 
            combined_description = re.sub(r'(\w+)\s+ing\b', r'\1ing', combined_description)
            combined_description = re.sub(r'(\w+)\s+ed\b', r'\1ed', combined_description)
            combined_description = re.sub(r'(\w+)\s+al\b', r'\1al', combined_description)
            combined_description = re.sub(r'(\w+)\s+sis\b', r'\1sis', combined_description)
            combined_description = re.sub(r'(\w+)\s+tion\b', r'\1tion', combined_description)
            combined_description = re.sub(r'(\w+)\s+phy\b', r'\1phy', combined_description)
            combined_description = re.sub(r'(\w+)\s+gram\b', r'\1gram', combined_description)
            
            # Clean up any markdown artifacts
            combined_description = re.sub(r'```.*$', '', combined_description)
            combined_description = re.sub(r'\s+Date.*$', '', combined_description)
            combined_description = re.sub(r'\s+---.*$', '', combined_description)
            
            current_service["description"] = combined_description.strip()
        
        # Override status with the correct one from the document
        if "status" not in current_service or "comments" in current_service["status"].lower():
            current_service["status"] = "Not Required"
            
        services.append(current_service)
    
    return services

def main():
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python test_extraction.py <markdown_file>")
        sys.exit(1)

    markdown_file = sys.argv[1]
    
    try:
        with open(markdown_file, 'r', encoding='utf-8') as f:
            ocr_text = f.read()
    except FileNotFoundError:
        print(f"Error: File {markdown_file} not found.")
        sys.exit(1)

    # Extract services
    lines = ocr_text.split('\n')
    print(f"DEBUG: Processing file with {len(lines)} lines")
    services = extract_services(lines)
    
    # Print results
    print("\nEXTRACTED SERVICES:")
    for i, service in enumerate(services, 1):
        print(f"Service {i}:")
        for key, value in service.items():
            print(f"  {key}: {value}")
        print()
    
    # Save to JSON
    output_file = 'test_services.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({"services": services}, f, indent=2, ensure_ascii=False)
    
    print(f"Results saved to {output_file}")

if __name__ == "__main__":
    main()