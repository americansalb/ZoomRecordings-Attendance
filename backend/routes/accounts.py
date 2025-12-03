from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import logging

from services.zoom_service import zoom_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def list_accounts():
    """
    List all configured Zoom accounts

    Returns list of accounts with their IDs and names
    """
    try:
        accounts = zoom_service.get_accounts()
        logger.info(f"GET /api/accounts - Returning {len(accounts)} account(s)")

        return {
            "accounts": accounts,
            "total": len(accounts)
        }

    except Exception as e:
        logger.error(f"GET /api/accounts - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users")
async def list_users(
    account_id: Optional[str] = Query(None, description="Zoom account ID (optional)")
):
    """
    List all users in the Zoom account

    Returns list of users with their IDs, names, and emails
    """
    try:
        users = await zoom_service.list_users(account_id)
        logger.info(f"GET /api/accounts/users - Returning {len(users)} user(s)")

        return {
            "users": users,
            "total": len(users)
        }

    except Exception as e:
        logger.error(f"GET /api/accounts/users - Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
