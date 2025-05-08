# Integration Guide: Service Extraction with OCR JSON Processing

This guide explains how to integrate the specialized service table extraction with the existing OCR JSON processing pipeline.

## Background

The medical form processing system has two components:

1. **OCR JSON Processor (`ocr_json.py`)**: Converts OCR text to structured JSON using AI, handles general form elements well.
2. **Service Extractor (`final_service_extractor.py`)**: Specialized module for accurately extracting service information from tables.

## Integration Options

There are three ways to integrate these components:

### 1. Use the Integrated Extractor (Recommended)

The `integrated_extractor.py` module combines both approaches:

```python
from integrated_extractor import process_form_with_enhanced_services

# Process a form with enhanced service extraction
enhanced_json = process_form_with_enhanced_services(
    "path/to/markdown.md", 
    "path/to/output.json"
)
```

This method:
- Runs the standard OCR JSON processing
- Then enhances the output with accurate service extraction
- Preserves the original structure while improving the service section

### 2. Modify the OCR JSON Pipeline

Add the service extraction directly to the `ocr_json.py` file:

```python
# Add this import at the top
from final_service_extractor import extract_service_with_codes, find_service_table_section

# Modify the convert_to_json function to use service extraction
def convert_to_json(ocr_text: str, file_name: str) -> Dict[str, Any]:
    # Existing code...
    
    # Process with GPT-4o as before
    structured_data = StructuredOCR(...)
    
    # Extract the raw text lines
    lines = ocr_text.split('\n')
    
    # Find service table and extract services
    table_lines = find_service_table_section(lines)
    if table_lines:
        services = extract_service_with_codes(table_lines)
        if services:
            # Convert service format
            formatted_services = []
            for service in services:
                # Format as needed...
                formatted_services.append(formatted_service)
            
            # Replace in the ocr_contents
            structured_data.ocr_contents.suggestedServices = formatted_services
    
    return structured_data.model_dump(exclude_none=True)
```

### 3. Post-Processing Approach

Run both processors separately and combine results:

```python
# Run the standard processor
from ocr_json import convert_to_json, extract_ocr_text, read_markdown_file

# Run the specialized service extractor
from final_service_extractor import process_form

# Combine the results
def process_with_dual_extraction(markdown_path):
    # Get standard extraction
    standard_json = convert_to_json(...)
    
    # Get specialized service extraction
    service_data = process_form(markdown_path)
    
    # Merge the results
    if "services" in service_data:
        standard_json["ocr_contents"]["suggestedServices"] = service_data["services"]
    
    return standard_json
```

## How the Service Extractor Works

The service extractor uses pattern matching and structural analysis to accurately identify:

1. Primary service codes (like `90911-00-00`)
2. Additional codes (like `14013`)
3. Service descriptions, properly joined from fragmented text
4. Numeric values (requested/approved quantities, costs)
5. Status and type information

It avoids hardcoding specific terms, focusing instead on patterns and structures in the data.

## Implementation Recommendations

1. Start with the `integrated_extractor.py` approach
2. Test with a variety of form examples
3. Adjust the mapping of service fields to match your data model
4. Consider adding a feedback mechanism to validate extraction accuracy

The integration preserves all the benefits of the OCR JSON processor while enhancing the accuracy of service extraction.