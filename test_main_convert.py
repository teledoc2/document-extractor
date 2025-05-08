from convert_to_json import read_markdown_file, extract_ocr_text, convert_to_json
import json
import os

# Read and process the file
file_path = "/Users/abdelfahmy/0dev/0CairoScan/scarpping/back/document-extractor/outputs/2025-03-20/J_A_GARCI.md"
file_name = os.path.basename(file_path)
markdown_content = read_markdown_file(file_path)
ocr_text = extract_ocr_text(markdown_content)

# Convert to JSON
json_data = convert_to_json(ocr_text, file_name)

# Check if nonStandardCode is in the suggestedServices
if "ocr_contents" in json_data and "suggestedServices" in json_data["ocr_contents"]:
    for i, service in enumerate(json_data["ocr_contents"]["suggestedServices"]):
        if "nonStandardCode" in service:
            print(f"Service {i+1} has nonStandardCode: {service['nonStandardCode']}")
        else:
            print(f"Service {i+1} does NOT have nonStandardCode field")

# Save the output
output_file = "main_convert_test.json"
with open(output_file, "w") as f:
    json.dump(json_data, f, indent=2)
print(f"\nFull output saved to {output_file}") 