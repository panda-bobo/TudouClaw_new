"""Persona management router — persona templates for agents."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.personas")

router = APIRouter(prefix="/api/portal", tags=["personas"])


@router.get("/personas")
async def list_personas(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all available persona templates."""
    try:
        from ...persona import list_personas as _list_personas
        return {"personas": _list_personas()}
    except (ImportError, Exception) as e:
        return {"personas": []}


@router.get("/personas/{persona_id}")
async def get_persona(
    persona_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get a single persona by ID."""
    try:
        from ...persona import get_persona as _get_persona
        p = _get_persona(persona_id)
        if not p:
            raise HTTPException(status_code=404, detail="Persona not found")
        return p.to_dict() if hasattr(p, "to_dict") else p
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(status_code=404, detail="Persona module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
