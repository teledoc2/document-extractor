#!/usr/bin/env python3
"""
Auto Form Filler for Patient Panel
=======================

This script automates filling a patient panel form on the MILLENSYS MiClinic website.
It fetches patient data (JSON) and a document (PDF) from a provided POST endpoint,
detects file types by extension, and uses Playwright to interact with the web form.

Usage:
    python script.py
    (Files are fetched from UNIFIED_ENDPOINT specified in .env)
"""

import sys
import json
import logging
import requests
import os
import re
import tempfile
from datetime import datetime
from browser_use import BrowserConfig, Browser
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from fuzzywuzzy import fuzz, process
from mimetypes import guess_extension
from pathlib import Path
from api import MICLINIC_UPLOAD_DIR, _cleanup_after_send
from dotenv import load_dotenv
from typing import Dict, Any

load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Load environment variables
UNIFIED_ENDPOINT = os.getenv("UNIFIED_ENDPOINT")
USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")

# Validate environment variables
if not UNIFIED_ENDPOINT:
    logger.warning("UNIFIED_ENDPOINT is not used anymore – reading from ./Uploads")
if not USERNAME or not PASSWORD:
    logger.error("USERNAME or PASSWORD not set in .env file")
    sys.exit(1)

# Construct sensitive_data from environment variables
sensitive_data = {
    "username": "Admin",
    "password": "@dm!n"
}

def extract_key_words(value: str) -> str:
    """Extracts key words from insurance names, handling parentheses, camelCase, and 'Al' prefixes."""
    if not value:
        return ""
    
    generic_terms = {"the", "and", "company", "reinsurance", "cooperative", "complex", "insurance"}
    value = value.replace("(", " ").replace(")", " ").strip()
    
    result = ""
    if value.lower().startswith("al") and len(value) > 2:
        result = "Al " + value[2:].lstrip()
    else:
        result = value
    
    final_result = ""
    for i, char in enumerate(result):
        if i > 0 and char.isupper() and result[i-1].islower():
            final_result += " " + char
        elif i > 0 and char.isupper() and result[i-1].isupper() and i < len(result)-1 and result[i+1].islower():
            final_result += " " + char
        else:
            final_result += char
    
    words = final_result.split()
    key_words = [word for word in words if word.lower() not in generic_terms]
    return " ".join(key_words)

def select_or_type_dropdown(page, dropdown_type: str, dropdown_input_xpath: str, list_id: str, value: str, dropdown_arrow_xpath: str = None, timeout: int = 20000) -> str:
    try:
        if not value:
            logger.warning(f"No {dropdown_type} value provided for dropdown at {dropdown_input_xpath}")
            return ""
        
        # ------------------------------------------------------------------
        # Fast-path for REFERRING: ignore input value, pick first option.
        # ------------------------------------------------------------------
        if dropdown_type == "referring":
            # Focus input and open dropdown (ArrowDown opens list & selects first item).
            if not find_element_with_fallback(page, dropdown_input_xpath, '//input[@name="Referring_input"]'):
                logger.error("Referring input field not found")
                return ""
            page.click(f'xpath={dropdown_input_xpath}')
            page.wait_for_timeout(300)
            page.press(f'xpath={dropdown_input_xpath}', "ArrowDown")
            page.wait_for_timeout(300)
            page.press(f'xpath={dropdown_input_xpath}', "Enter")
            page.wait_for_timeout(800)

            # Capture first option text if list is still present
            list_xpath = f"//ul[@id='{list_id}']"
            try:
                options = page.query_selector_all(f'xpath={list_xpath}/li/span[@class="k-cell"][2]')
                selected = options[0].inner_text().strip() if options else value
            except Exception:
                selected = value
            logger.info(f"Referring fast-path selected: '{selected}'")
            return selected
        
        key_input = extract_key_words(value)
        logger.info(f"Extracted key words for {dropdown_type} '{value}': '{key_input}'")

        if dropdown_type == "carrier_type":
            fallback_xpath = '//input[@name="OrganizationId_input"]'
            initial_wait = 1000
        elif dropdown_type == "visit_type":
            fallback_xpath = '//input[@name="VisitType_input"]'
            initial_wait = 2000
        elif dropdown_type == "carrier":
            fallback_xpath = '//input[@name="ContractId_input"]'
            initial_wait = 1000
        else:
            raise ValueError(f"Unknown dropdown_type: {dropdown_type}")

        if find_element_with_fallback(page, dropdown_input_xpath, fallback_xpath):
            page.click(f'xpath={dropdown_input_xpath}')
            page.wait_for_timeout(initial_wait)
        else:
            logger.error(f"{dropdown_type} input field not found at {dropdown_input_xpath}")
            return key_input

        key_words = key_input.split()
        if dropdown_type in ["carrier_type", "carrier"]:
            max_chunk_size = 2
        else:
            max_chunk_size = 3

        paren_chunks = []
        paren_words = set()
        paren_matches = re.findall(r'\((.*?)\)', value)
        for match in paren_matches:
            match_words = extract_key_words(match).split()
            for size in range(1, len(match_words) + 1):
                for i in range(len(match_words) - size + 1):
                    chunk = " ".join(match_words[i:i + size])
                    paren_chunks.append(chunk)
                    if size == 1:
                        paren_words.add(chunk)

        all_chunks = []
        for size in range(1, max_chunk_size + 1):
            for i in range(len(key_words) - size + 1):
                chunk = " ".join(key_words[i:i + size])
                all_chunks.append(chunk)

        chunks_by_length = {size: [] for size in range(1, max_chunk_size + 1)}
        for chunk in all_chunks:
            length = len(chunk.split())
            chunks_by_length[length].append(chunk)

        ordered_chunks = []
        for size in [2, 3, 1]:
            if size <= max_chunk_size:
                paren_in_size = [chunk for chunk in chunks_by_length[size] if chunk in paren_chunks]
                other_in_size = [chunk for chunk in chunks_by_length[size] if chunk not in paren_chunks]
                ordered_chunks.extend(paren_in_size + other_in_size)
        
        for size in range(4, max_chunk_size + 1):
            if size in chunks_by_length:
                paren_in_size = [chunk for chunk in chunks_by_length[size] if chunk in paren_chunks]
                other_in_size = [chunk for chunk in chunks_by_length[size] if chunk not in paren_chunks]
                ordered_chunks.extend(paren_in_size + other_in_size)

        if 1 in chunks_by_length:
            paren_single = [chunk for chunk in chunks_by_length[1] if chunk in paren_words]
            other_single = [chunk for chunk in chunks_by_length[1] if chunk not in paren_words]
            ordered_chunks = [c for c in ordered_chunks if len(c.split()) != 1] + (paren_single + other_single)

        chunks = ordered_chunks
        logger.info(f"Text chunks for {dropdown_type}: {chunks}")

        list_xpath = f"//ul[@id='{list_id}']"
        for chunk in chunks:
            logger.info(f"Typing chunk: '{chunk}'")
            page.press(f'xpath={dropdown_input_xpath}', "Control+a")
            page.press(f'xpath={dropdown_input_xpath}', "Backspace")
            page.fill(f'xpath={dropdown_input_xpath}', chunk)
            page.wait_for_timeout(2000)

            try:
                page.wait_for_selector(f'xpath={list_xpath}', state='visible', timeout=timeout)
                break
            except PlaywrightTimeoutError:
                logger.error(f"{dropdown_type} dropdown {list_xpath} not visible after {timeout}ms with '{chunk}'")
                if chunk == chunks[-1]:
                    return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, key_input)
                continue

        if dropdown_type == "referring":
            # Fetch visible options (each item is inside a span.k-cell[2]).
            options = page.query_selector_all(f'xpath={list_xpath}/li/span[@class="k-cell"][2]')
            available_options = [option.inner_text().strip() for option in options if option.inner_text().strip()]

            if not available_options:
                logger.warning(f"No options loaded for referring at {list_xpath}")
                return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, key_input)

            # Choose the first option in the list for referring.
            page.press(f'xpath={dropdown_input_xpath}', "ArrowDown")
            page.wait_for_timeout(300)
            page.press(f'xpath={dropdown_input_xpath}', "Enter")
            page.wait_for_timeout(2000)
            selected_value = available_options[0]
            logger.info(f"Selected referring (first option): '{selected_value}'")
            return selected_value
        else:
            available_options = log_available_options(page, list_xpath)
            logger.info(f"Available options in dropdown at {list_xpath}: {available_options}")

            if not available_options:
                logger.warning(f"No options loaded for {dropdown_type} at {list_xpath}")
                return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, key_input)

        if dropdown_type in ["carrier_type", "carrier"]:
            cleaned_options = []
            for option in available_options:
                parts = option.split("-")
                if len(parts) >= 3:
                    cleaned_option = "-".join(parts[2:]).strip()
                elif len(parts) == 2:
                    cleaned_option = parts[1].strip()
                else:
                    cleaned_option = option
                cleaned_options.append(cleaned_option)
        else:
            cleaned_options = [opt.replace("-", " ").replace(",", " ").replace("(", " ").replace(")", " ").strip() for opt in available_options]
        logger.info(f"Cleaned options for fuzzy matching: {cleaned_options}")

        best_match_cleaned = None
        best_score = 0
        best_chunk = None
        for chunk in chunks:
            match, score = process.extractOne(chunk, cleaned_options, scorer=fuzz.token_sort_ratio)
            if score > best_score:
                best_match_cleaned = match
                best_score = score
                best_chunk = chunk
        logger.info(f"Best fuzzy match for chunks '{chunks}': '{best_match_cleaned}' with score {best_score} (from chunk '{best_chunk}')")

        if best_score >= 60:
            original_match, original_score = process.extractOne(key_input, cleaned_options, scorer=fuzz.token_sort_ratio)
            logger.info(f"Double-check with original '{key_input}': '{original_match}' with score {original_score}")

            if original_score >= 50:
                best_match_index = cleaned_options.index(best_match_cleaned)
                best_match = available_options[best_match_index]
            elif original_score > best_score:
                best_match_index = cleaned_options.index(original_match)
                best_match = available_options[best_match_index]
                logger.info(f"Overriding chunk match with original match '{original_match}' (score {original_score} > {best_score})")
            else:
                best_match_index = cleaned_options.index(best_match_cleaned)
                best_match = available_options[best_match_index]

            if dropdown_type in ["carrier_type", "carrier"]:
                parts = best_match.split("-")
                if len(parts) >= 3:
                    type_value = "-".join(parts[2:]).strip()
                elif len(parts) == 2:
                    type_value = parts[1].strip()
                else:
                    type_value = best_match

                page.press(f'xpath={dropdown_input_xpath}', "Control+a")
                page.press(f'xpath={dropdown_input_xpath}', "Backspace")
                page.fill(f'xpath={dropdown_input_xpath}', type_value)
                page.wait_for_timeout(2000)

                available_options = log_available_options(page, list_xpath)
                logger.info(f"Options after typing '{type_value}': {available_options}")

                if best_match in available_options:
                    target_index = available_options.index(best_match)
                    logger.info(f"Target option '{best_match}' found at index {target_index}")
                    
                    page.click(f'xpath={dropdown_input_xpath}')
                    page.wait_for_timeout(500)

                    for _ in range(target_index + 1):
                        page.press(f'xpath={dropdown_input_xpath}', "ArrowDown")
                        page.wait_for_timeout(500)
                    
                    page.press(f'xpath={dropdown_input_xpath}', "Enter")
                    page.wait_for_timeout(2000)
                    logger.info(f"Selected {dropdown_type}: '{best_match}' using keyboard navigation")
                    return best_match
                else:
                    logger.warning(f"'{best_match}' not found in available options after typing '{type_value}': {available_options}")
                    page.press(f'xpath={dropdown_input_xpath}', "Control+a")
                    page.press(f'xpath={dropdown_input_xpath}', "Backspace")
                    page.fill(f'xpath={dropdown_input_xpath}', type_value)
                    page.wait_for_timeout(2000)
                    page.press(f'xpath={dropdown_input_xpath}', "Enter")
                    page.wait_for_timeout(2000)
                    logger.info(f"Selected {dropdown_type}: '{type_value}' using fallback type and enter")
                    return type_value
            else:
                type_value = best_match
                page.press(f'xpath={dropdown_input_xpath}', "Control+a")
                page.press(f'xpath={dropdown_input_xpath}', "Backspace")
                page.fill(f'xpath={dropdown_input_xpath}', type_value)
                page.wait_for_timeout(2000)
                page.press(f'xpath={dropdown_input_xpath}', "Enter")
                page.wait_for_timeout(2000)
                logger.info(f"Selected {dropdown_type}: '{type_value}' from match '{best_match}' (score: {best_score if original_score < 50 or original_score <= best_score else original_score})")
                return type_value
        else:
            logger.warning(f"No {dropdown_type} match above threshold 60 for '{chunks}' (best: '{best_match_cleaned}', score: {best_score})")
            return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, key_input)

    except Exception as e:
        logger.error(f"Failed to process {dropdown_type} at {dropdown_input_xpath}: {str(e)}", exc_info=True)
        if page.is_closed():
            logger.error("Page is closed, cannot proceed with fallback.")
            return ""
        return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, extract_key_words(value))

def find_element_with_fallback(page, primary_xpath: str, fallback_selector: str, label_text: str = None, timeout: int = 10000) -> bool:
    try:
        page.wait_for_selector(f'xpath={primary_xpath}', state='visible', timeout=timeout)
        return True
    except PlaywrightTimeoutError:
        logger.warning(f"Primary XPath {primary_xpath} not found. Attempting fallback...")
        try:
            if label_text:
                fallback_xpath = f'//label[contains(text(), "{label_text}")]/following-sibling::input | //label[contains(text(), "{label_text}")]/following-sibling::select'
                page.wait_for_selector(f'xpath={fallback_xpath}', state='visible', timeout=timeout)
                return True
            page.wait_for_selector(fallback_selector, state='visible', timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            logger.error(f"Fallback selector {fallback_selector} also not found.")
            return False

def log_available_options(page, list_xpath: str, has_nested_span_p: bool = False, timeout: int = 10000) -> list:
    try:
        page.wait_for_selector(f'xpath={list_xpath}', state='visible', timeout=timeout)
        if has_nested_span_p:
            options = page.query_selector_all(f'xpath={list_xpath}/li/span/p')
            available_options = [option.inner_text().strip() for option in options if option.inner_text().strip()]
        else:
            options = page.query_selector_all(f'xpath={list_xpath}/li')
            available_options = [option.inner_text().strip() for option in options if option.inner_text().strip()]
        logger.info(f"Available options in dropdown at {list_xpath}: {available_options}")
        return available_options
    except Exception as e:
        logger.error(f"Failed to log available options at {list_xpath}: {str(e)}", exc_info=True)
        return []

def retry_operation(page, action, max_attempts, value, xpath, timeout=2000):
    for attempt in range(max_attempts):
        try:
            return action()
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1}/{max_attempts} to select {value} failed: {str(e)}" if "select" in action.__name__ else f"Attempt {attempt + 1}/{max_attempts} to type {value} failed: {str(e)}")
            if attempt == max_attempts - 1:
                logger.error(f"Failed to {'select' if 'select' in action.__name__ else 'type'} {value} after all attempts: {str(e)}", exc_info=True)
                return ""
            page.wait_for_timeout(timeout)

def select_kendo_dropdown_by_arrow(page, dropdown_arrow_xpath: str, list_id: str, value: str, has_nested_span_p: bool = False, timeout: int = 20000) -> str:
    def select_action():
        if not value:
            logger.warning(f"No value provided for Kendo dropdown at {dropdown_arrow_xpath}")
            return ""
        if find_element_with_fallback(page, dropdown_arrow_xpath, f'//span[contains(@class, "k-select")]'):
            page.click(f'xpath={dropdown_arrow_xpath}')
            page.wait_for_timeout(1000)
            page.wait_for_timeout(5000)
        else:
            logger.error(f"Arrow button not found at {dropdown_arrow_xpath}")
            return ""
        list_xpath = f"//ul[@id='{list_id}']"
        page.wait_for_selector(f'xpath={list_xpath}', state='visible', timeout=timeout)
        available_options = log_available_options(page, list_xpath, has_nested_span_p)
        if value not in available_options:
            logger.warning(f"Value '{value}' not found in dropdown options: {available_options}")
            return ""
        option_xpath = f"{list_xpath}/li[span/p[text()='{value}']]" if has_nested_span_p else f"{list_xpath}/li[text()='{value}']"
        option_element = page.wait_for_selector(f'xpath={option_xpath}', state='visible', timeout=timeout)
        option_element.scroll_into_view_if_needed()
        page.wait_for_timeout(1000)
        page.click(f'xpath={option_xpath}')
        page.wait_for_timeout(1000)
        page.wait_for_timeout(3000)
        logger.info(f"Selected {value} in Kendo dropdown at {dropdown_arrow_xpath}")
        return value

    return retry_operation(page, select_action, max_attempts=5, value=value, xpath=dropdown_arrow_xpath)

def type_and_enter_kendo_dropdown(page, dropdown_input_xpath: str, value: str, timeout: int = 10000) -> str:
    def type_action():
        if not value:
            logger.warning(f"No value provided for Kendo dropdown input at {dropdown_input_xpath}")
            return ""
        if find_element_with_fallback(page, dropdown_input_xpath, f'//input[@name="{value}_input"]'):
            page.click(f'xpath={dropdown_input_xpath}')
            page.wait_for_timeout(2000)
            page.press(f'xpath={dropdown_input_xpath}', "Control+a")
            page.wait_for_timeout(500)
            page.press(f'xpath={dropdown_input_xpath}', "Backspace")
            page.wait_for_timeout(500)
            page.fill(f'xpath={dropdown_input_xpath}', value)
            page.wait_for_timeout(1000)
            page.press(f'xpath={dropdown_input_xpath}', "Enter")
            page.wait_for_timeout(300)                 # short pause
            page.press(f'xpath={dropdown_input_xpath}', "Tab")
            page.wait_for_timeout(5000)
            logger.info(f"Typed and entered {value} in Kendo dropdown at {dropdown_input_xpath}")
            return value
        else:
            logger.error(f"Input field not found at {dropdown_input_xpath}")
            return ""

    return retry_operation(page, type_action, max_attempts=3, value=value, xpath=dropdown_input_xpath)

def set_date_of_birth(page, input_xpath: str, target_date: str, timeout: int = 10000) -> bool:
    try:
        date_obj = datetime.strptime(target_date, "%m/%d/%Y")
        target_month = f"{date_obj.month:02d}"
        target_day = f"{date_obj.day:02d}"
        target_year = str(date_obj.year)

        if find_element_with_fallback(page, input_xpath, input_xpath, timeout=timeout):
            logger.info(f"Focusing Date of Birth field at {input_xpath}")
            page.click(f'xpath={input_xpath}')
            page.wait_for_timeout(2000)
        else:
            logger.error(f"Date of Birth input not found at {input_xpath}")
            return False

        page.click(f'xpath={input_xpath}', position={"x": 5, "y": 5})
        page.wait_for_timeout(2000)
        for char in target_month:
            page.type(f'xpath={input_xpath}', char)
            page.wait_for_timeout(200)
        page.wait_for_timeout(2000)

        page.click(f'xpath={input_xpath}', position={"x": 30, "y": 5})
        page.wait_for_timeout(2000)
        for char in target_day:
            page.type(f'xpath={input_xpath}', char)
            page.wait_for_timeout(200)
        page.wait_for_timeout(2000)

        page.click(f'xpath={input_xpath}', position={"x": 60, "y": 5})
        page.wait_for_timeout(2000)
        for char in target_year:
            page.type(f'xpath={input_xpath}', char)
            page.wait_for_timeout(200)
        page.wait_for_timeout(2000)

        logger.info(f"Set Date of Birth to {target_date}")
        return True
    except Exception as e:
        logger.error(f"Failed to set Date of Birth to {target_date}: {str(e)}", exc_info=True)
        return False

def input_icd10_codes(page, input_xpath: str, codes: list, timeout: int = 10000) -> bool:
    try:
        if find_element_with_fallback(page, input_xpath, input_xpath, timeout=timeout):
            logger.info(f"Focusing ICD-10 input field at {input_xpath}")
            page.click(f'xpath={input_xpath}')
            page.wait_for_timeout(1000)
        else:
            logger.error(f"ICD-10 input not found at {input_xpath}")
            return False

        for code in codes:
            if code:
                icd10_code = code.split("-")[0].strip()
                if " " in icd10_code:
                    icd10_code = icd10_code.split(" ")[0].strip()
                
                for char in icd10_code:
                    page.type(f'xpath={input_xpath}', char)
                    page.wait_for_timeout(200)
                page.wait_for_timeout(4000)
                page.press(f'xpath={input_xpath}', "Enter")
                page.wait_for_timeout(1000)
                logger.info(f"Entered ICD-10 code: {icd10_code}")
        return True
    except Exception as e:
        logger.error(f"Failed to input ICD-10 codes: {str(e)}", exc_info=True)
        return False

def select_or_type_modality(page, dropdown_arrow_xpath: str, dropdown_input_xpath: str, list_id: str, value: str, timeout: int = 20000) -> str:
    try:
        if not value:
            logger.warning(f"No Modality value provided for dropdown at {dropdown_arrow_xpath}")
            return ""
        
        cleaned_value = extract_key_words(value)
        logger.info(f"Extracted key words from '{value}': '{cleaned_value}'")

        cleaned_value = cleaned_value.replace("-", " ").replace("(", " ").replace(")", " ").replace(".", " ").replace(",", " ").strip()
        cleaned_value = " ".join(cleaned_value.split())
        logger.info(f"Cleaned value after removing special characters: '{cleaned_value}'")

        input_words = cleaned_value.split()
        logger.info(f"Input split into words: {input_words}")

        if find_element_with_fallback(page, dropdown_arrow_xpath, f'//span[contains(@class, "k-select")]'):
            page.click(f'xpath={dropdown_arrow_xpath}')
            page.wait_for_timeout(1000)
        else:
            logger.error(f"Arrow button not found at {dropdown_arrow_xpath}")
            return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, value)

        list_xpath = f"//ul[@id='{list_id}']"
        page.wait_for_selector(f'xpath={list_xpath}', state='visible', timeout=timeout)
        available_options = log_available_options(page, list_xpath)
        
        cleaned_options = {}
        for option in available_options:
            modality_name = option.split("-")[0].strip()
            cleaned_name = modality_name.replace("(", " ").replace(")", " ").replace(".", " ").replace(",", " ").strip()
            cleaned_name = " ".join(cleaned_name.split())
            cleaned_options[cleaned_name] = option
        logger.info(f"Cleaned options: {list(cleaned_options.keys())}")

        option_scores = {}
        for cleaned_opt, full_opt in cleaned_options.items():
            opt_words = cleaned_opt.split()
            matches = 0
            for input_word in input_words:
                for opt_word in opt_words:
                    score = fuzz.ratio(input_word.lower(), opt_word.lower())
                    if score >= 90:
                        matches += 1
                        break
            option_scores[full_opt] = matches
        
        if option_scores:
            best_match = max(option_scores.items(), key=lambda x: x[1])[0]
            match_count = option_scores[best_match]
            logger.info(f"Best match: '{best_match}' with {match_count} word matches")
            
            if match_count > 0:
                option_xpath = f"{list_xpath}/li[text()='{best_match}']"
                try:
                    option_element = page.wait_for_selector(f'xpath={option_xpath}', state='visible', timeout=timeout)
                    option_element.scroll_into_view_if_needed()
                    page.wait_for_timeout(1000)
                    page.click(f'xpath={option_xpath}')
                    page.wait_for_timeout(1000)
                    logger.info(f"Selected matching modality from list: '{best_match}' with {match_count} word matches")
                    return best_match
                except:
                    logger.warning(f"Could not select '{best_match}' from list, falling back to typing")
                    page.click(f'xpath={dropdown_input_xpath}')
                    page.wait_for_timeout(500)
                    page.press(f'xpath={dropdown_input_xpath}', "Control+a")
                    page.press(f'xpath={dropdown_input_xpath}', "Backspace")
                    page.fill(f'xpath={dropdown_input_xpath}', best_match)
                    page.wait_for_timeout(1000)
                    page.press(f'xpath={dropdown_input_xpath}', "Enter")
                    page.wait_for_timeout(1000)
                    logger.info(f"Typed and entered modality: '{best_match}'")
                    return best_match
        
        logger.warning(f"No options with strong word matches found for '{cleaned_value}'")
        return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, value)

    except Exception as e:
        logger.error(f"Failed to process modality......................................................................................: {str(e)}", exc_info=True)
        return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, value)

def select_or_type_service_desc(page, dropdown_arrow_xpath: str, dropdown_input_xpath: str, list_id: str, value: str, timeout: int = 20000) -> str:
    try:
        if not value:
            logger.warning(f"No service description value provided for dropdown at {dropdown_arrow_xpath}")
            return ""
        
        cleaned_value = extract_key_words(value)
        logger.info(f"Extracted key words from '{value}': '{cleaned_value}'")

        cleaned_value = cleaned_value.replace("-", " ").replace("(", " ").replace(")", " ").replace(".", " ").replace(",", " ").strip()
        noise_phrases = ['refer to other hospital', 'for', 'with', 'and']
        for phrase in noise_phrases:
            cleaned_value = re.sub(rf'\b{phrase}\b', ' ', cleaned_value, flags=re.IGNORECASE)
        cleaned_value = " ".join(cleaned_value.split())
        logger.info(f"Cleaned value after removing special characters and noise: '{cleaned_value}'")

        if "-" in value:
            parts = value.split("-")
            last_part = parts[-1].strip()
            paren_match = re.search(r'\((.*?)\)\s*(.*)', last_part)
            if paren_match:
                code, text_after = paren_match.groups()
                if text_after.strip():
                    cleaned_value = text_after.strip().replace("-", " ").replace("(", " ").replace(")", " ").replace(".", " ").replace(",", " ").strip()
                    cleaned_value = " ".join(cleaned_value.split())
                elif code.strip().replace(".", "").isalnum():
                    cleaned_value = last_part.split("(")[0].strip().replace("-", " ").replace("(", " ").replace(")", " ").replace(".", " ").replace(",", " ").strip()
                    cleaned_value = " ".join(cleaned_value.split())
            else:
                cleaned_value = parts[-1].strip().replace("-", " ").replace("(", " ").replace(")", " ").replace(".", " ").replace(",", " ").strip()
                cleaned_value = " ".join(cleaned_value.split())
        logger.info(f"Final cleaned value for service description: '{cleaned_value}'")

        # ------------------------------------------------------------------
        # Determine if we have a modality keyword mapping (CT/MR/US). If so we
        # can skip the complex chunk logic and just open the list and pick.
        # ------------------------------------------------------------------
        lower_val_full = value.lower() if value else ""
        mapping = {
            "CT": ["computerised", "computerized", "computed tomography", "computed"],
            "MR": ["magnetic resonance"],
            "US": ["ultrasound"],
        }
        target_acronym = None
        for acr, terms in mapping.items():
            if any(t in lower_val_full for t in terms):
                target_acronym = acr
                break

        if target_acronym:
            # Open dropdown list via arrow button
            if dropdown_arrow_xpath and find_element_with_fallback(page, dropdown_arrow_xpath, f'//span[contains(@class, "k-select")]'):
                page.click(f'xpath={dropdown_arrow_xpath}')
            else:
                page.click(f'xpath={dropdown_input_xpath}')
                page.press(f'xpath={dropdown_input_xpath}', "ArrowDown")
            page.wait_for_timeout(600)

            list_xpath = f"//ul[@id='{list_id}']"
            page.wait_for_selector(f'xpath={list_xpath}', state='visible', timeout=timeout)
            available_options = log_available_options(page, list_xpath)
            match_opt = next((opt for opt in available_options if target_acronym.lower() in opt.lower()), None)
            if match_opt:
                idx = available_options.index(match_opt)
                for _ in range(idx):
                    page.press(f'xpath={dropdown_input_xpath}', "ArrowDown")
                page.press(f'xpath={dropdown_input_xpath}', "Enter")
                page.wait_for_timeout(800)
                logger.info(f"Service_desc fast-path selected '{match_opt}' for acronym '{target_acronym}'")
                return match_opt
            else:
                logger.warning(f"No option contained '{target_acronym}', falling back.")

        # ----- Existing chunk/fuzzy logic continues if no fast-path matched -----

        key_words = cleaned_value.split()
        max_chunk_size = 3
        all_chunks = []
        for size in range(1, max_chunk_size + 1):
            for i in range(len(key_words) - size + 1):
                chunk = " ".join(key_words[i:i + size])
                all_chunks.append(chunk)
        
        chunks_by_length = {size: [] for size in range(1, max_chunk_size + 1)}
        for chunk in all_chunks:
            length = len(chunk.split())
            chunks_by_length[length].append(chunk)
        
        ordered_chunks = []
        for size in [2, 3, 1]:
            ordered_chunks.extend(chunks_by_length.get(size, []))
        logger.info(f"Text chunks for service description: {ordered_chunks}")

        list_xpath = f"//ul[@id='{list_id}']"
        for chunk in ordered_chunks:
            logger.info(f"Typing chunk: '{chunk}'")
            if find_element_with_fallback(page, dropdown_input_xpath, '//input[@name="ServiceNameId_input"]'):
                page.click(f'xpath={dropdown_input_xpath}')
                page.wait_for_timeout(1000)
                page.press(f'xpath={dropdown_input_xpath}', "Control+a")
                page.press(f'xpath={dropdown_input_xpath}', "Backspace")
                page.fill(f'xpath={dropdown_input_xpath}', chunk)
                page.wait_for_timeout(2000)
            else:
                logger.error(f"Service description input field not found at {dropdown_input_xpath}")
                return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, cleaned_value)

            try:
                page.wait_for_selector(f'xpath={list_xpath}', state='visible', timeout=timeout)
                break
            except PlaywrightTimeoutError:
                logger.warning(f"Dropdown {list_xpath} not visible after {timeout}ms with chunk '{chunk}'")
                if chunk == ordered_chunks[-1]:
                    logger.warning(f"No chunks loaded the dropdown list. Falling back to typing '{cleaned_value}'")
                    return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, cleaned_value)
                continue

        available_options = log_available_options(page, list_xpath)
        if not available_options:
            logger.warning(f"No options loaded for service description at {list_xpath}")
            return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, cleaned_value)

        # Keyword-based modality mapping: CT / MR / US
        lower_val = value.lower() if value else ""
        acronym_map = {
            "CT": ["computerised", "computerized", "computed tomography", "computed"],
            "MR": ["magnetic resonance"],
            "US": ["ultrasound"],
        }

        target_acronym = None
        for acr, terms in acronym_map.items():
            if any(term in lower_val for term in terms):
                target_acronym = acr
                break

        if target_acronym:
            logger.info(f"Keyword mapping triggered – selecting first option containing '{target_acronym}'.")
            matched_opt = next((opt for opt in available_options if target_acronym.lower() in opt.lower()), None)
            if matched_opt:
                idx = available_options.index(matched_opt)
                page.click(f'xpath={dropdown_input_xpath}')
                page.wait_for_timeout(300)
                for _ in range(idx + 1):
                    page.press(f'xpath={dropdown_input_xpath}', "ArrowDown")
                    page.wait_for_timeout(150)
                page.press(f'xpath={dropdown_input_xpath}', "Enter")
                page.wait_for_timeout(1000)
                logger.info(f"Selected service description via keyword mapping: '{matched_opt}'")
                return matched_opt
            else:
                logger.warning(f"No dropdown option contained acronym '{target_acronym}'. Proceeding with fuzzy matching.")
        
        cleaned_options = []
        for option in available_options:
            modality_name = option.split("-", 1)[-1].strip() if "-" in option else option
            cleaned_name = modality_name.replace("(", " ").replace(")", " ").replace(".", " ").replace(",", " ").strip()
            cleaned_name = " ".join(cleaned_name.split())
            cleaned_options.append(cleaned_name)
        logger.info(f"Cleaned options for fuzzy matching: {cleaned_options}")

        best_match_cleaned = None
        best_score = 0
        best_chunk = None
        for chunk in ordered_chunks:
            match, score = process.extractOne(chunk, cleaned_options, scorer=fuzz.token_sort_ratio)
            if score > best_score:
                best_match_cleaned = match
                best_score = score
                best_chunk = chunk
        logger.info(f"Best fuzzy match for chunks '{ordered_chunks}': '{best_match_cleaned}' with score {best_score} (from chunk '{best_chunk}')")

        original_match, original_score = process.extractOne(cleaned_value, cleaned_options, scorer=fuzz.token_sort_ratio)
        logger.info(f"Double-check with original '{cleaned_value}': '{original_match}' with score {original_score}")

        if best_score >= 60 or original_score >= 60:
            if original_score >= 50 and (original_score > best_score or best_score < 60):
                best_match_index = cleaned_options.index(original_match)
                best_match = available_options[best_match_index]
                logger.info(f"Overriding chunk match with original match '{original_match}' (score {original_score} > {best_score})")
            else:
                best_match_index = cleaned_options.index(best_match_cleaned)
                best_match = available_options[best_match_index]

            type_value = best_match.split("-", 1)[-1].strip() if "-" in best_match else best_match
            logger.info(f"Typing cleaned best match: '{type_value}'")

            page.click(f'xpath={dropdown_input_xpath}')
            page.wait_for_timeout(1000)
            page.press(f'xpath={dropdown_input_xpath}', "Control+a")
            page.press(f'xpath={dropdown_input_xpath}', "Backspace")
            page.fill(f'xpath={dropdown_input_xpath}', type_value)
            page.wait_for_timeout(2000)

            available_options = log_available_options(page, list_xpath)
            logger.info(f"Options after typing '{type_value}': {available_options}")

            if best_match in available_options:
                target_index = available_options.index(best_match)
                logger.info(f"Target option '{best_match}' found at index {target_index}")

                page.click(f'xpath={dropdown_input_xpath}')
                page.wait_for_timeout(500)

                for _ in range(target_index + 1):
                    page.press(f'xpath={dropdown_input_xpath}', "ArrowDown")
                    page.wait_for_timeout(500)

                page.press(f'xpath={dropdown_input_xpath}', "Enter")
                page.wait_for_timeout(2000)
                logger.info(f"Selected service description: '{best_match}' using keyboard navigation")
                return best_match
            else:
                logger.warning(f"'{best_match}' not found in available options after typing '{type_value}': {available_options}")
                page.press(f'xpath={dropdown_input_xpath}', "Control+a")
                page.press(f'xpath={dropdown_input_xpath}', "Backspace")
                page.fill(f'xpath={dropdown_input_xpath}', type_value)
                page.wait_for_timeout(1000)
                page.press(f'xpath={dropdown_input_xpath}', "Enter")
                page.wait_for_timeout(2000)
                logger.info(f"Selected service description: '{type_value}' using fallback type and enter")
                return type_value
        else:
            logger.warning(f"No service description match above threshold 60 for '{ordered_chunks}' (best: '{best_match_cleaned}', score: {best_score})")
            return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, cleaned_value)

    except Exception as e:
        logger.error(f"Failed to process service description at {dropdown_arrow_xpath}: {str(e)}", exc_info=True)
        return type_and_enter_kendo_dropdown(page, dropdown_input_xpath, cleaned_value if 'cleaned_value' in locals() else value)

def get_latest_files(upload_dir: Path) -> tuple[str | None, str | None]:
    """Return paths of the latest JSON and PDF present in *upload_dir*.

    If either type is missing, the corresponding return value is None.
    """
    json_files = list(upload_dir.glob("*.json"))
    pdf_files  = list(upload_dir.glob("*.pdf"))

    latest_json = max(json_files, key=lambda p: p.stat().st_mtime, default=None)
    latest_pdf  = max(pdf_files,  key=lambda p: p.stat().st_mtime, default=None)

    return str(latest_json) if latest_json else None, str(latest_pdf) if latest_pdf else None

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type(requests.exceptions.RequestException))
def load_patient_data(json_file: str) -> dict:
    """
    Loads patient data from the JSON file.
    
    Args:
        json_file: Path to the JSON file.
    
    Returns:
        Patient data from the JSON file.
        
    Raises:
        ValueError: If JSON file is invalid or not found.
    """
    logger.info(f"Loading patient data from: {json_file}")
    try:
        if not json_file or not os.path.exists(json_file):
            logger.error(f"Не удалось найти файл JSON: {json_file}")
            raise ValueError("JSON file not found")
        
        with open(json_file, 'r', encoding='utf-8') as f:
            patient_data = json.load(f)
        logger.info("Successfully loaded JSON data from file.")
        logger.debug(f"JSON content: {json.dumps(patient_data, indent=2)}")
        return patient_data

    except json.JSONDecodeError:
        logger.error(f"Invalid JSON format in file: {json_file}")
        raise ValueError("Invalid JSON format")
    except Exception as e:
        logger.error(f"Error processing JSON file: {str(e)}")
        raise ValueError(f"Error processing JSON file: {str(e)}")

def upload_document(page, document_type: str, document_path: str, timeout: int = 50000) -> bool:
    try:
        upload_button_xpath = '//button[@id="uploadDocsForService"]'
        if find_element_with_fallback(page, upload_button_xpath, '//button[contains(text(), "Upload")]'):
            page.click(f'xpath={upload_button_xpath}')
            page.wait_for_timeout(2000)
            logger.info("Clicked Upload Documents button")
        else:
            logger.error("Upload Documents button not found")
            return False

        dialog_xpath = '//div[@id="UploadDocsForServiceWindow"]'
        page.wait_for_selector(f'xpath={dialog_xpath}', state='visible', timeout=timeout)
        logger.info("Upload Documents dialog opened")

        document_type_input_xpath = '//input[@name="documenttypeforService_visitreg_input"]'
        document_type_list_id = "documenttypeforService_visitreg_listbox"
        result = select_kendo_dropdown_by_arrow(
            page,
            dropdown_arrow_xpath='//span[@aria-controls="documenttypeforService_visitreg_listbox"]',
            list_id=document_type_list_id,
            value=document_type
        )
        if result:
            logger.info(f"Selected Document Type: {result}")
        else:
            logger.warning(f"Failed to select Document Type: {document_type}")
            return False

        if not document_path or not os.path.exists(document_path):
            logger.error(f"Document file not found: {document_path}")
            return False

        file_input_xpath = '//input[@id="filesvisitregForServcie"]'
        if find_element_with_fallback(page, file_input_xpath, '//input[@name="filesvisitregForServcie"]'):
            page.set_input_files(f'xpath={file_input_xpath}', document_path)
            page.wait_for_timeout(15000)
            logger.info(f"Uploaded file: {document_path}")
        else:
            logger.error("File input field not found")
            return False

        page.wait_for_timeout(8000)
        logger.info("Waited for a few seconds after file upload")

        close_button_xpath = '//button[contains(@onclick, "closeuploadDocsForServicewindow")]'
        if find_element_with_fallback(page, close_button_xpath, '//button[contains(text(), "Close")]'):
            page.click(f'xpath={close_button_xpath}')
            page.wait_for_timeout(2000)
            logger.info("Closed Upload Documents dialog")
        else:
            logger.error("Close button not found")
            return False

        return True

    except Exception as e:
        logger.error(f"Failed to upload document: {str(e)}")
        return False

# New helper function to flatten JSON
def flatten_json(json_data: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
    items = []
    for k, v in json_data.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_json(v, new_key, sep=sep).items())
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    items.extend(flatten_json(item, f"{new_key}[{i}]", sep=sep).items())
                else:
                    items.append((f"{new_key}[{i}]", item))
        else:
            items.append((new_key, v))
    return dict(items)

# New helper function to find fields dynamically
def find_field(flat_json: Dict[str, Any], field_name: str) -> Any:
    candidates = [v for k, v in flat_json.items() if k.lower().endswith(f".{field_name.lower()}")]
    if candidates:
        # Prefer the deepest path if multiple matches exist
        deepest_path = max((k for k in flat_json if k.lower().endswith(f".{field_name.lower()}")), key=lambda x: x.count('.'))
        return flat_json[deepest_path]
    return None

# Dynamic field mapping dictionary
FIELD_MAPPING = {
    "first_name": {
        "xpath": '//input[@id="GName"]',
        "fallback": '//input[@name="GName"]',
        "label": "First Name",
        "method": "fill",
        "log_message": "Filled First Name: {}"
    },
    "middle_name": {
        "xpath": '//input[@id="MName"]',
        "fallback": '//input[@name="MName"]',
        "label": "Middle Name",
        "method": "fill",
        "log_message": "Filled Middle Name: {}"
    },
    "last_name": {
        "xpath": '//input[@id="FName"]',
        "fallback": '//input[@name="FName"]',
        "label": "Last Name",
        "method": "fill",
        "log_message": "Filled Last Name: {}"
    },
    "gender": {
        "xpath": '//input[@name="GenderId_input"]',
        "method": "type_and_enter_kendo_dropdown",
        "log_message": "Selected Gender: {}"
    },
    "dob": {
        "xpath": '//input[@id="DateTimeOfBirth"]',
        "method": "set_date_of_birth",
        "log_message": "Set Date of Birth to {}"
    },
    "id_type_and_document_id": {
        "id_type_xpath": "//span[@aria-controls='IdentityTypeId_listbox']",
        "id_type_list_id": "IdentityTypeId_listbox",
        "document_id_xpath": '//input[@id="SsnNumber"]',
        "document_id_fallback": '//input[@name="SsnNumber"]',
        "method": "custom_id_type_and_document_id",
        "log_message": "Filled Document ID: {} with ID Type: {}"
    },
    "mobile_number": {
        "xpath": '//input[@id="PersonalMobileNumber"]',
        "fallback": '//input[@name="PersonalMobileNumber"]',
        "label": "Mobile",
        "method": "fill",
        "log_message": "Filled Personal Mobile Number: {}"
    },
    "nationality": {
        "xpath": "//span[@aria-controls='NationalityId_listbox']",
        "list_id": "NationalityId_listbox",
        "method": "select_kendo_dropdown_by_arrow",
        "log_message": "Selected Nationality: {}"
    },
    "marital_status": {
        "xpath": "//span[@aria-controls='MaritalStatusId_listbox']",
        "list_id": "MaritalStatusId_listbox",
        "method": "select_kendo_dropdown_by_arrow",
        "log_message": "Selected Marital Status: {}"
    },
    "modality": {
        "dropdown_arrow_xpath": "//span[@aria-controls='VisitLocationID_listbox']",
        "dropdown_input_xpath": '//input[@aria-owns="VisitLocationID_listbox"]',
        "list_id": "VisitLocationID_listbox",
        "method": "select_or_type_modality",
        "log_message": "Processed Modality: {}"
    },
    "referring": {
        "dropdown_input_xpath": '//input[@name="Referring_input"]',
        "list_id": "Referring_listbox",
        "dropdown_type": "referring",
        "timeout": 15000,
        "method": "select_or_type_dropdown",
        "log_message": "Processed Referring: {}"
    },
    "visit_type": {
        "dropdown_input_xpath": '//input[@aria-owns="VisitAdmissionTypeID_listbox"]',
        "list_id": "VisitAdmissionTypeID_listbox",
        "dropdown_type": "visit_type",
        "timeout": 15000,
        "method": "select_or_type_dropdown",
        "log_message": "Processed Visit Admission Type: {}"
    },
    "icd10_codes": {
        "xpath": '//input[@aria-controls="Icd10_listbox"]',
        "method": "input_icd10_codes",
        "log_message": "Entered ICD-10 codes"
    },
    "patient_class": {
        "xpath": "//span[@aria-controls='PatientClassID_listbox']",
        "list_id": "PatientClassID_listbox",
        "method": "select_kendo_dropdown_by_arrow",
        "log_message": "Selected Patient Class: {}"
    },
    "chief_complaint": {
        "xpath": '//input[@aria-owns="chiefcomplaint_listbox"]',
        "method": "type_and_enter_kendo_dropdown",
        "log_message": "Selected Chief Complaint: {}"
    },
    "carrier_type": {
        "dropdown_input_xpath": '//input[@name="OrganizationId_input"]',
        "list_id": "OrganizationId_listbox",
        "dropdown_arrow_xpath": "//span[@aria-controls='OrganizationId_listbox']",
        "dropdown_type": "carrier_type",
        "method": "select_or_type_dropdown",
        "log_message": "Processed Carrier Type: {}"
    },
    "carrier": {
        "dropdown_input_xpath": '//input[@name="ContractId_input"]',
        "list_id": "ContractId_listbox",
        "dropdown_arrow_xpath": "//span[@aria-controls='ContractId_listbox']",
        "dropdown_type": "carrier",
        "method": "select_or_type_dropdown",
        "log_message": "Processed Carrier: {}"
    },
    "policy_no": {
        "xpath": '//input[@id="MemberName"]',
        "fallback": '//input[@name="MemberName"]',
        "method": "fill",
        "log_message": "Filled Policy Number: {}"
    },
    "membership_no": {
        "xpath": '//input[@id="InsuranceNumber"]',
        "fallback": '//input[@name="InsuranceNumber"]',
        "method": "fill",
        "log_message": "Filled Membership Number: {}"
    },
    "approval_no": {
        "xpath": '//input[@id="DocumentNumber"]',
        "fallback": '//input[@name="DocumentNumber"]',
        "method": "fill",
        "log_message": "Filled Approval Number: {}"
    },
    "service_desc": {
        "dropdown_arrow_xpath": "//span[@aria-controls='ServiceNameId_listbox']",
        "dropdown_input_xpath": '//input[@aria-owns="ServiceNameId_listbox"]',
        "list_id": "ServiceNameId_listbox",
        "method": "select_or_type_service_desc",
        "log_message": "Processed Service Procedure: {}"
    },
    "upload_document": {
        "xpath": '//button[@id="uploadDocsForService"]',
        "method": "upload_document",
        "log_message": "Uploaded document of type: {}"
    },
    "status": {
        "xpath": "//input[@aria-owns='ServiceStatus_listbox']",
        "method": "type_and_enter_kendo_dropdown",
        "log_message": "Selected Service Status: {}"
    },
    "patient_value": {
        "xpath": '//input[@id="visitreg_patientvaluetext"]',
        "fallback": '//input[@name="visitreg_patientvaluetext"]',
        "method": "fill",
        "log_message": "Filled Patient Value: {}"
    },
    "notes_additional": {
        "xpath": '//textarea[@id="Description"]',
        "method": "fill",
        "log_message": "Filled Additional Comments: {}"
    },
    "more_patient_controls": {
        "xpath": '//*[@id="morepatientcontrolsbtn"]',
        "fallback": '//button[contains(text(), "More Patient Controls")]',
        "method": "click",
        "log_message": "Clicked More Patient Controls button"
    },
    "more_visit_info": {
        "xpath": '//*[@id="visitreg_morevisitinfobtn"]',
        "fallback": '//a[contains(text(), "Hide")]',
        "method": "click",
        "log_message": "Clicked More Visit Info button"
    },
    "more_services_info": {
        "xpath": '//*[@id="visitreg_moreserviceinfobtn"]',
        "fallback": '//button[contains(text(), "More")]',
        "method": "click",
        "log_message": "Clicked More Services Info button"
    },
    "add_service": {
        "xpath": '//*[@id="visitreg_addservice"]',
        "fallback": '//button[contains(text(), "Add Service")]',
        "method": "click",
        "log_message": "Clicked Add Service button"
    },
    "save": {
        "xpath": '//*[@id="visitreg_savevisit"]',
        "fallback": '//button[contains(text(), "Save")]',
        "method": "click",
        "log_message": "Clicked Save button"
    }
}

def process_field(page, field_name: str, value, extra_args=None):
    """Dynamically process a field based on its mapping."""
    mapping = FIELD_MAPPING.get(field_name)
    if not mapping:
        logger.warning(f"No mapping found for field: {field_name}")
        return

    method = mapping["method"]
    xpath = mapping.get("xpath")
    log_message = mapping.get("log_message")

    if not value and method not in ["click", "upload_document"]:
        logger.warning(f"No value provided for {field_name}")
        return

    if method == "fill":
        if find_element_with_fallback(page, xpath, mapping.get("fallback"), mapping.get("label")):
            page.fill(xpath, str(value))
            page.wait_for_timeout(1000)
            logger.info(log_message.format(value))
    elif method == "type_and_enter_kendo_dropdown":
        result = type_and_enter_kendo_dropdown(page, xpath, value)
        if result:
            logger.info(log_message.format(result))
    elif method == "set_date_of_birth":
        if set_date_of_birth(page, xpath, value):
            logger.info(log_message.format(value))
    elif method == "select_kendo_dropdown_by_arrow":
        result = select_kendo_dropdown_by_arrow(page, xpath, mapping["list_id"], value)
        if result:
            logger.info(log_message.format(result))
    elif method == "select_or_type_dropdown":
        result = select_or_type_dropdown(
            page,
            dropdown_type=mapping.get("dropdown_type"),
            dropdown_input_xpath=mapping["dropdown_input_xpath"],
            list_id=mapping["list_id"],
            value=value,
            dropdown_arrow_xpath=mapping.get("dropdown_arrow_xpath"),
            timeout=mapping.get("timeout", 20000)
        )
        if result:
            logger.info(log_message.format(result))
            page.wait_for_timeout(1500 if field_name in ["referring", "visit_type", "carrier"] else 3300 if field_name == "carrier_type" else 0)
    elif method == "select_or_type_modality":
        result = select_or_type_modality(
            page,
            dropdown_arrow_xpath=mapping["dropdown_arrow_xpath"],
            dropdown_input_xpath=mapping["dropdown_input_xpath"],
            list_id=mapping["list_id"],
            value=value
        )
        if result:
            logger.info(log_message.format(result))
    elif method == "select_or_type_service_desc":
        result = select_or_type_service_desc(
            page,
            dropdown_arrow_xpath=mapping["dropdown_arrow_xpath"],
            dropdown_input_xpath=mapping["dropdown_input_xpath"],
            list_id=mapping["list_id"],
            value=value
        )
        if result:
            logger.info(log_message.format(result))
    elif method == "input_icd10_codes":
        if input_icd10_codes(page, xpath, value):
            logger.info(log_message)
    elif method == "upload_document":
        result = upload_document(
            page,
            document_type=value["document_type"],
            document_path=value["document_path"]
        )
        if result:
            logger.info(log_message.format(value["document_type"]))
    elif method == "click":
        if find_element_with_fallback(page, xpath, mapping.get("fallback")):
            page.click(f'xpath={xpath}')
            page.wait_for_timeout(2000)
            logger.info(log_message)

def find_key_recursive(data, keys, depth=0, max_depth=10):
    """Recursively search for any key in keys (case-insensitive) in a dictionary or list."""
    if depth > max_depth:
        return None
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() in [key.lower() for key in keys]:
                return v
            if isinstance(v, (dict, list)):
                result = find_key_recursive(v, keys, depth + 1, max_depth)
                if result is not None:
                    return result
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                result = find_key_recursive(item, keys, depth + 1, max_depth)
                if result is not None:
                    return result
    return None

def main():
    # Create a temporary directory for file storage
    with tempfile.TemporaryDirectory() as temp_dir:
        # ------------------------------------------------------------------
        # Fetch the newest JSON/PDF that were just uploaded to the shared
        # directory.  They should have been saved there moments earlier by the
        # /upload endpoint in *api.py* which also triggered this script.
        # ------------------------------------------------------------------
        json_file, pdf_file = get_latest_files(MICLINIC_UPLOAD_DIR)

        if not json_file:
            logger.error("No JSON file found in uploads directory.")
            print("Error: No JSON file found in uploads directory.")
            sys.exit(1)

        # Optionally copy to temp_dir if mutations were required; currently the
        # script only *reads* the JSON/PDF, so we can work directly on the
        # originals.  That lets us archive them at the end without worrying
        # about mismatched paths.

        # Load patient data from JSON
        try:
            patient_data = load_patient_data(json_file)
        except Exception as e:
            logger.error(f"Failed to load patient data: {str(e)}")
            print(f"Error: Failed to load patient data: {str(e)}")
            sys.exit(1)

        # Flatten the JSON for flexible field extraction
        flat_json = flatten_json(patient_data)
        logger.debug(f"Flattened JSON: {json.dumps(flat_json, indent=2)}")

        # Dynamically extract fields
        insured_name_raw = find_field(flat_json, "insuredName") or find_field(flat_json, "name")
        if not insured_name_raw:
            logger.error("No 'insuredName' or 'name' found in JSON; cannot proceed without patient name")
            print("Error: No patient name found in JSON")
            sys.exit(1)
        insured_name = insured_name_raw.split()
        first_name = insured_name[0] if insured_name else ""
        middle_name = insured_name[1] if len(insured_name) > 2 else ""
        last_name = " ".join(insured_name[2 if len(insured_name) > 2 else 1:]) if len(insured_name) > 1 else ""

        gender = find_field(flat_json, "sex") or find_field(flat_json, "gender")
        gender_value = 'M' if gender and gender.lower() == "male" else 'F' if gender and gender.lower() == "female" else 'O'

        raw_age = find_field(flat_json, "age")
        try:
            if raw_age:
                age_str = raw_age.lower().replace("years old", "").replace("years", "").replace("year", "").strip()
                age = int(age_str)
            else:
                age = 0
        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to parse age '{raw_age}': {str(e)}")
            age = 0

        raw_date_of_visit = find_field(flat_json, "dateOfVisit") or "01/03/2025"
        visit_year = None
        date_formats = ["%d/%m/%Y %I:%M:%S %p", "%Y-%m-%d", "%d-%m-%Y %I:%M %p", "%d/%m/%Y"]
        for date_format in date_formats:
            try:
                date_of_visit = datetime.strptime(raw_date_of_visit, date_format)
                visit_year = date_of_visit.year
                break
            except ValueError:
                continue
        if visit_year is None:
            logger.warning(f"Failed to parse dateOfVisit '{raw_date_of_visit}'. Using 2025.")
            visit_year = 2025

        try:
            birth_year = visit_year - age if age else visit_year
            dob = f"01/01/{birth_year}"
        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to calculate DOB: {str(e)}")
            dob = f"01/01/{visit_year}"

        document_id = find_field(flat_json, "nationalId") or ""
        nationality_value = "Saudi" if document_id.startswith("1") else "Foreigner" if document_id.startswith("2") else ""
        id_type = "ID" if document_id.startswith("1") else "Iqama" if document_id.startswith("2") else ""

        marital_status_raw = "Unknown"
        if find_field(flat_json, "married"):
            marital_status_raw = "Married"
        elif find_field(flat_json, "single"):
            marital_status_raw = "Single"

        # Extract description and note for modality and service_desc from the same dictionary
        
# Extract description and note from the first dictionary in suggestedServices under ocr_contents
        description = ""
        note = ""
        # First try to find ocr_contents and then suggestedServices within it
        ocr_contents = find_key_recursive(patient_data, ["ocr_contents", "ocr_content", "OCR_CONTENTS", "OCR_CONTENT"])
        if ocr_contents:
            suggested_services = find_key_recursive(ocr_contents, ["suggestedServices", "SuggestedServices", "SUGGESTEDSERVICES"]) or []
        else:
            # Fallback to searching for suggestedServices directly
            suggested_services = find_key_recursive(patient_data, ["suggestedServices", "SuggestedServices", "SUGGESTEDSERVICES"]) or []
        logger.info(f"Raw suggestedServices: {json.dumps(suggested_services, indent=2)}")
        if isinstance(suggested_services, list) and len(suggested_services) > 0 and isinstance(suggested_services[0], dict):
            service = suggested_services[0]
            # Flexible key matching for description and note
            description = next((service.get(key, "") for key in ["description", "Description", "DESCRIPTION"]), "")
            note = next((service.get(key, "") for key in ["note", "Note", "NOTE"]), "")
        else:
            logger.warning("No valid dictionary found at suggestedServices[0]. Full patient_data:")
            logger.warning(f"{json.dumps(patient_data, indent=2)}")
        # Combine description and note
        service_text = f"{description} {note}".strip()
        if not service_text:
            logger.warning("No valid 'description' or 'note' pair found in suggestedServices[0]")
            service_text = ""
        modality_value = service_text
        service_desc = service_text

        provider_name_raw = find_field(flat_json, "providerName") or ""
        if provider_name_raw:
            cleaned_name = " ".join([word for word in provider_name_raw.replace("-", " ").replace(",", " ").split() if not word.isdigit()])
            referral = cleaned_name if cleaned_name else ""
        else:
            referral = ""

        icd10_codes = [
            find_field(flat_json, "principalCode") or "",
            find_field(flat_json, "secondCode") or "",
            find_field(flat_json, "thirdCode") or "",
            find_field(flat_json, "fourthCode") or "",
            find_field(flat_json, "fifthCode") or "",
            find_field(flat_json, "sixthCode") or ""
        ]

        patient_class = "Outpatient" if find_field(flat_json, "outpatient") else "Unknown" if not find_field(flat_json, "inpatient") else "Inpatient"

        chief_complaint_raw = find_field(flat_json, "chiefComplaints") or ""
        if chief_complaint_raw:
            parts = chief_complaint_raw.split(" - ")
            cleaned_parts = []
            for part in parts:
                part = part.strip("()")
                if "-" in part and part.split("-")[0].strip().replace(".", "").isalnum():
                    cleaned_parts.append(part.split("-", 1)[1].strip())
                else:
                    cleaned_parts.append(part.strip())
            chief_complaint_value = " ".join(cleaned_parts)
        else:
            chief_complaint_value = ""

        policy_no = find_field(flat_json, "policyNo") or ""
        membership_no = find_field(flat_json, "idCardNo") or ""
        approval_no = find_field(flat_json, "approvalReferrenceNumber") or ""
        insurance_company = find_field(flat_json, "insuranceCompanyName") or ""

        document_upload = {
            "document_type": "History",
            "document_path": pdf_file if pdf_file else ""
        }
        patient_value = 0.0
        status_value = "Arrived"
        notes_additional = find_field(flat_json, "comments") or ""
        mobile_number = "9876543210"

        config = BrowserConfig(headless=False, disable_security=True)
        
        with sync_playwright() as p:
            try:
                browser = Browser(config=config)
                playwright_browser = p.chromium.launch(headless=config.headless)
                page = playwright_browser.new_page()
                logger.info("Browser launched successfully.")
                print("Browser launched successfully.")
            except Exception as e:
                logger.error(f"Failed to launch browser: {str(e)}", exc_info=True)
                print(f"Error: Failed to launch browser: {str(e)}")
                raise

            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    logger.info(f"Attempting login (Attempt {attempt + 1}/{max_attempts})")
                    print(f"Attempting login (Attempt {attempt + 1}/{max_attempts})")
                    page.goto("http://77.30.174.26/MILLENSYS/MiClinic/Account/LogOn", timeout=80000)
                    page.wait_for_load_state('networkidle', timeout=80000)
                    page.fill('//input[@id="username"]', sensitive_data["username"])
                    page.wait_for_timeout(1000)
                    page.fill('//input[@id="password"]', sensitive_data["password"])
                    page.wait_for_timeout(1000)
                    if find_element_with_fallback(page, '/html/body/div/div[3]/div/div/div/form/div/div/div/div[1]/div[2]/div[2]/div[5]/div[2]', '//button[@type="submit"]'):
                        page.click('xpath=/html/body/div/div[3]/div/div/div/form/div/div/div/div[1]/div[2]/div[2]/div[5]/div[2]')
                    page.wait_for_timeout(1000)
                    page.wait_for_url("http://77.30.174.26/MILLENSYS/MiClinic/CommonPages/PatientPanel", timeout=80000)
                    logger.info("Logged in successfully.")
                    print("Logged in successfully.")
                    page.wait_for_timeout(2000)
                    break
                except PlaywrightTimeoutError as e:
                    logger.error(f"Timeout during login attempt {attempt + 1}: {str(e)}", exc_info=True)
                    if attempt == max_attempts - 1:
                        logger.error("All login attempts failed due to timeout.")
                        print("Error: All login attempts failed due to timeout.")
                        playwright_browser.close()
                        raise
                    page.wait_for_timeout(5000)
                except Exception as e:
                    logger.error(f"Failed to login on attempt {attempt + 1}: {str(e)}", exc_info=True)
                    if attempt == max_attempts - 1:
                        logger.error("All login attempts failed.")
                        print("Error: All login attempts failed.")
                        playwright_browser.close()
                        raise
                    page.wait_for_timeout(5000)

            try:
                fields_to_process = [
                    ("first_name", first_name),
                    ("middle_name", middle_name),
                    ("last_name", last_name),
                    ("gender", gender_value),
                    ("dob", dob),
                    ("id_type_and_document_id", (document_id, id_type)),
                    ("document_id", document_id),
                    ("mobile_number", mobile_number),
                    ("nationality", nationality_value),
                    ("more_patient_controls", None),
                    ("marital_status", marital_status_raw),
                    ("modality", modality_value),
                    ("referring", referral),
                    ("visit_type", referral),
                    ("icd10_codes", icd10_codes),
                    ("more_visit_info", None),
                    ("patient_class", patient_class),
                    ("chief_complaint", chief_complaint_value),
                    ("carrier_type", insurance_company),
                    ("carrier", insurance_company),
                    ("policy_no", policy_no),
                    ("membership_no", membership_no),
                    ("approval_no", approval_no),
                    ("service_desc", service_desc),
                    ("status", status_value),
                    ("upload_document", document_upload),
                    ("patient_value", patient_value),
                    ("more_services_info", None),
                    ("notes_additional", notes_additional),
                    ("add_service", None),
                    ("save", None)
                ]

                for field_name, value in fields_to_process:
                    print(f"Processing {field_name.replace('_', ' ').title()}...")
                    if field_name == "id_type_and_document_id" and value[0]:
                        document_id, id_type = value
                        mapping = FIELD_MAPPING[field_name]
                        select_kendo_dropdown_by_arrow(page, mapping["id_type_xpath"], mapping["id_type_list_id"], id_type)
                        if find_element_with_fallback(page, mapping["document_id_xpath"], mapping["document_id_fallback"]):
                            page.fill(mapping["document_id_xpath"], document_id)
                            page.wait_for_timeout(1000)
                            logger.info(mapping["log_message"].format(document_id, id_type))
                    else:
                        process_field(page, field_name, value)

                page.wait_for_timeout(3000)
                logger.info("Patient Panel form filled successfully.")
                print("Patient Panel form filled successfully!")
            except Exception as e:
                logger.error(f"Failed to fill form: {str(e)}", exc_info=True)
                print(f"Error: Failed to fill form: {str(e)}")
                raise
            try:
                playwright_browser.close()
                logger.info("Browser closed successfully.")
                print("Browser closed successfully.")
            except Exception as e:
                logger.error(f"Failed to close browser: {str(e)}", exc_info=True)
                print(f"Error: Failed to close browser: {str(e)}")

            # ------------------------------------------------------------------
            # Archive the processed files so they won't be picked up again on the
            # next run.  We call the same helper used by FastAPI.
            # ------------------------------------------------------------------
            try:
                paths_to_archive = [Path(p) for p in [json_file, pdf_file] if p]
                _cleanup_after_send(paths_to_archive)
            except Exception as exc:
                logger.error(f"Failed to archive processed files: {exc}")

    print("Patient panel form filled and submitted successfully!")

if __name__ == "__main__":
    main()