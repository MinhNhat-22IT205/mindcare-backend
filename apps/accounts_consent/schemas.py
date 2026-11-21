from __future__ import annotations

from typing import Optional
from ninja import Schema


class ConsentDTO(Schema):
    allowAiCheckinPersonalize: bool = True
    allowAiJournalContext: bool = False
    allowAutoCrisisSos: bool = False
    shareCoarseLocationOnSos: bool = False
    sosFeatureEnabled: bool = True


class TrustedContactDTO(Schema):
    id: str
    name: str
    relationship: str
    phone: str
    avatarLetter: str
    status: str = "confirmed"
    permissionScope: str = "Chỉ nhận tín hiệu hỗ trợ khi có SOS"
    onlyOnSos: bool = True
    shareLocation: bool = False
    allowAutoCrisisSos: bool = False
    neverShareJournal: bool = True


class TrustedContactInput(Schema):
    id: Optional[str] = None
    name: str
    relationship: str
    phone: str
    avatarLetter: Optional[str] = None
    status: Optional[str] = "confirmed"
    permissionScope: Optional[str] = "Chỉ nhận tín hiệu hỗ trợ khi có SOS"
    onlyOnSos: bool = True
    shareLocation: bool = False
    allowAutoCrisisSos: bool = False
    neverShareJournal: Optional[bool] = True


class TrustedContactUpdateInput(Schema):
    name: Optional[str] = None
    relationship: Optional[str] = None
    phone: Optional[str] = None
    avatarLetter: Optional[str] = None
    status: Optional[str] = None
    permissionScope: Optional[str] = None
    onlyOnSos: Optional[bool] = None
    shareLocation: Optional[bool] = None
    allowAutoCrisisSos: Optional[bool] = None
    neverShareJournal: Optional[bool] = None


class DeleteResultDTO(Schema):
    deleted: bool
