#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Data models for form extraction systems.
Contains both medical form models and radiology registration form models.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union
from enum import Enum

# Simplified language enum with just English and Arabic
class Language(str, Enum):
    ENGLISH = "English"
    ARABIC = "Arabic"

###########################################
# Medical Form Models
###########################################

# Define detailed Pydantic models matching the JSON structure
class ProviderInfo(BaseModel):
    """Healthcare provider information."""
    providerName: Optional[str] = None
    insuranceCompanyName: Optional[str] = None
    tpaCompanyName: Optional[str] = None
    patientFileNumber: Optional[str] = None
    dept: Optional[str] = None
    single: Optional[bool] = None
    married: Optional[bool] = None
    planType: Optional[str] = None
    dateOfVisit: Optional[str] = None
    newVisit: Optional[bool] = None
    followUp: Optional[bool] = None
    refill: Optional[bool] = None
    walkIn: Optional[bool] = None
    referral: Optional[bool] = None
    approvalDateTime: Optional[str] = None
    approvalValidity: Optional[str] = None


class InsuredInfo(BaseModel):
    """Insured person information."""
    insuredName: Optional[str] = None
    documentId: Optional[str] = None
    idCardNo: Optional[str] = None
    nationalId: Optional[str] = None
    policyNo: Optional[str] = None
    memberSince: Optional[str] = None
    memberType: Optional[str] = None
    expiryDate: Optional[str] = None
    policyHolder: Optional[str] = None
    class_: Optional[str] = Field(None, alias="class")
    approval: Optional[str] = None
    approvalReferrenceNumber: Optional[str] = None
    approvalStatus: Optional[str] = None
    approvalType: Optional[str] = None
    message: Optional[str] = None
    adjudicationPayer: Optional[str] = None
    payer: Optional[str] = None

class PatientInfo(BaseModel):
    """Patient demographic information."""
    sex: Optional[str] = None
    age: Optional[str] = None
    gender: Optional[str] = None

class VisitDetails(BaseModel):
    """Medical visit details completed by attending physician."""
    inpatient: Optional[bool] = None
    outpatient: Optional[bool] = None
    emergencyCase: Optional[bool] = None
    emergencyCareLevel: Optional[str] = None
    physicianName: Optional[str] = None
    bp: Optional[str] = None
    pulse: Optional[str] = None
    temperature: Optional[str] = None
    weight: Optional[str] = None
    height: Optional[str] = None
    rr: Optional[str] = None
    durationOfIllness: Optional[str] = None
    chiefComplaints: Optional[str] = None
    significantSigns: Optional[str] = None
    possibleLineOfTreatment: Optional[str] = None
    otherConditions: Optional[str] = None

class EmergencyCareLevel(BaseModel):
    """Emergency care level information."""
    level_1: Optional[bool] = None
    level_2: Optional[bool] = None
    level_3: Optional[bool] = None

class DiagnosisInfo(BaseModel):
    """Diagnosis information."""
    diagnosis: Optional[str] = None
    principalCode: Optional[str] = None
    secondCode: Optional[str] = None
    thirdCode: Optional[str] = None
    fourthCode: Optional[str] = None
    fifthCode: Optional[str] = None
    sixthCode: Optional[str] = None

class ManagementInfo(BaseModel):
    """Treatment management information."""
    chronic: Optional[bool] = None
    congenital: Optional[bool] = None
    rta: Optional[bool] = None
    workRelated: Optional[bool] = None
    vaccination: Optional[bool] = None
    checkUp: Optional[bool] = None
    psychiatric: Optional[bool] = None
    infertility: Optional[bool] = None
    pregnancy: Optional[bool] = None
    indicateLmp: Optional[str] = None
    
class ServicesTable(BaseModel):
    """Vertical list of Medical service or procedure item."""
    codeService: Optional[float] = Field(None, alias="(code) service")
    reqQty: Optional[float] = Field(None, alias="Req.Qty")
    reqCost: Optional[float] = Field(None, alias="Req.Cost")
    grossAmount: Optional[float] = Field(None, alias="Gross Amount")
    appQty: Optional[float] = Field(None, alias="App.Qty")
    appCost: Optional[float] = Field(None, alias="App.Cost")
    appGross: Optional[float] = Field(None, alias="App.Gross")
    note: Optional[str] = None

class CompletedByInfo(BaseModel):
    """Form completion information."""
    providerApproval: Optional[str] = None
    completedCodedBy: Optional[str] = None
    signature: Optional[str] = None
    date: Optional[str] = None

class MedicationInfo(BaseModel):
    """Medication information."""
    medicationName: Optional[str] = None
    type: Optional[str] = None
    reqQty: Optional[float] = None
    reqCost: Optional[float] = None
    grossAmount: Optional[float] = None
    appQty: Optional[int] = None
    appCost: Optional[float] = None
    appGross: Optional[float] = None
    note: Optional[str] = None

class CaseManagementForm(BaseModel):
    """Case management information."""
    caseManagementFormIncluded: Optional[bool] = None
    possibleLineOfManagement: Optional[str] = None
    expectedDateOfAdmission: Optional[str] = None
    estimatedCost: Optional[float] = None
    estimatedGross: Optional[float] = None
    totalApprovedCost: Optional[float] = None
    estimatedLengthOfStay: Optional[str] = None
    approvedLengthOfStay: Optional[str] = None
    providerComments: Optional[str] = None


class CertificationInfo(BaseModel):
    """Certification and signature information."""
    physicianCertification: Optional[str] = None
    physicianSignature: Optional[bool] = None
    physicianSignatureDate: Optional[str] = None
    patientCertification: Optional[str] = None
    patientSignature: Optional[bool] = None
    patientSignatureDate: Optional[str] = None
    patientRelationship: Optional[str] = None


class InsuranceApproval(BaseModel):
    """Insurance approval information."""
    approved: Optional[bool] = None
    notApproved: Optional[bool] = None
    approvalNo: Optional[str] = None
    approvalValidity: Optional[str] = None
    comments: Optional[str] = None
    approvedDisapprovedBy: Optional[str] = None
    signature: Optional[str] = None
    date: Optional[str] = None

class MedicalFormContent(BaseModel):
    provider: Optional[ProviderInfo] = None
    insured: Optional[InsuredInfo] = None
    patient: Optional[PatientInfo] = None
    visitDetails: Optional[VisitDetails] = None
    emergencyLevel: Optional[List[EmergencyCareLevel]] = None
    diagnosis: Optional[DiagnosisInfo] = None
    management: Optional[ManagementInfo] = None
    services: Optional[List[ServicesTable]] = None
    completedBy: Optional[CompletedByInfo] = None
    medications: Optional[List[MedicationInfo]] = None
    caseManagementForm: Optional[CaseManagementForm] = None
    certification: Optional[CertificationInfo] = None
    insuranceApproval: Optional[InsuranceApproval] = None

class StructuredOCR(BaseModel):
    file_name: str
    topics: List[str]
    languages: List[Language]
    ocr_contents: MedicalFormContent
    document_type: Optional[str] = None
    confidence_score: Optional[float] = None
    processing_time: Optional[float] = None
    page_count: Optional[int] = None
    extracted_text_length: Optional[int] = None

# print(StructuredOCR.model_json_schema())