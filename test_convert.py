#!/usr/bin/env python3
"""Test script for convert_to_json functionality."""

import sys
import json
import os
from convert_to_json import extract_simple_services, convert_to_json

def main():
    """Test the extraction on a real markdown file."""
    if len(sys.argv) < 2:
        print("Usage: python test_convert.py <markdown_file>")
        sys.exit(1)

    markdown_file = sys.argv[1]
    
    try:
        with open(markdown_file, 'r', encoding='utf-8') as f:
            ocr_text = f.read()
    except FileNotFoundError:
        print(f"Error: File {markdown_file} not found.")
        sys.exit(1)

    # Extract lines
    lines = ocr_text.split('\n')
    print(f"Processing file with {len(lines)} lines\n")
    
    # Simple extraction
    services = extract_simple_services(lines)
    
    # Print detailed results
    print("SIMPLE SERVICE EXTRACTION:")
    for i, service in enumerate(services, 1):
        print(f"Service {i}:")
        for key, value in service.items():
            print(f"  {key}: {value}")
        print()
    
    # Save simple extraction to JSON
    with open('test_convert_simple.json', 'w', encoding='utf-8') as f:
        json.dump({"services": services}, f, indent=2, ensure_ascii=False)
    
    # Full conversion
    file_name = os.path.basename(markdown_file)
    json_data = convert_to_json(ocr_text, file_name)
    
    # DEBUG: Print info about services before conversion to JSON
    print("\nDEBUG - SERVICES BEFORE FINAL CONVERSION:")
    for i, service in enumerate(services, 1):
        print(f"Service {i}:")
        for key, value in service.items():
            print(f"  {key}: {value}")
        print()
    
    # Modify json_data to use our extracted services
    if json_data and "ocr_contents" in json_data:
        formatted_services = []
        
        for service in services:
            formatted_service = {
                "code": service.get("code", ""),
                "description": service.get("description", ""),
                "additionalCodes": service.get("additionalCodes", [])
            }
            
            # Include nonStandardCode if present
            if "nonStandardCode" in service:
                formatted_service["nonStandardCode"] = service["nonStandardCode"]
            
            # Map numeric fields
            if "reqQty" in service:
                formatted_service["requestedQuantity"] = service["reqQty"]
            if "reqCost" in service:
                formatted_service["requestedCost"] = service["reqCost"]
            if "grossAmount" in service:
                formatted_service["grossAmount"] = service["grossAmount"]
            if "appQty" in service:
                formatted_service["approvedQuantity"] = service["appQty"]
            if "appCost" in service:
                formatted_service["approvedCost"] = service["appCost"]
            if "appGross" in service:
                formatted_service["approvedGross"] = service["appGross"]
            
            # Add type if available
            if "type" in service:
                formatted_service["serviceType"] = service["type"]
            
            # Add status if available
            if "status" in service:
                formatted_service["status"] = service["status"]
            
            # Add note if available
            if "note" in service:
                formatted_service["note"] = service["note"]
            
            formatted_services.append(formatted_service)
            
        # Replace services in the JSON
        json_data["ocr_contents"]["suggestedServices"] = formatted_services
        
    # Print full conversion results
    print("\nFULL CONVERSION RESULT:")
    print(json.dumps(json_data, indent=2, ensure_ascii=False))
    
    # Save full conversion to JSON
    with open('test_convert_full.json', 'w', encoding='utf-8') as f:
        json.dump(json_data, f, indent=2, ensure_ascii=False)
    
    # Debug: Print suggestedServices from final JSON output 
    print("\nDEBUG - SUGGESTED SERVICES IN FINAL JSON:")
    if json_data and "ocr_contents" in json_data and "suggestedServices" in json_data["ocr_contents"]:
        for i, service in enumerate(json_data["ocr_contents"]["suggestedServices"], 1):
            print(f"Service {i}:")
            for key, value in service.items():
                print(f"  {key}: {value}")
            print()
    
    print(f"Results saved to test_convert_simple.json and test_convert_full.json")

if __name__ == "__main__":
    main() 