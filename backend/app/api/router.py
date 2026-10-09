from fastapi import APIRouter, Depends

from app.core.security import get_current_actor
from app.modules.access.api.router import router as access_router
from app.modules.attachments.api.router import router as attachments_router
from app.modules.change_sets.api.router import router as change_sets_router
from app.modules.audit.api.router import router as audit_router
from app.modules.dictionaries.api.router import router as dictionaries_router
from app.modules.entities.api.router import router as entity_schemas_router
from app.modules.entities.api.metadata import router as metadata_router
from app.modules.excel.api.router import router as excel_router
from app.modules.geocoding.api.router import router as geocoding_router
from app.modules.imports.api.router import router as imports_router
from app.modules.objects.api.generated import router as generated_api_router
from app.modules.notifications.api.router import router as notifications_router
from app.modules.objects.api.router import router as entity_objects_router
from app.modules.relations.api.router import router as relations_router
from app.modules.search.api.router import router as search_router
from app.modules.users.api.router import router as users_router
from app.modules.workflows.api.router import router as workflows_router

api_router = APIRouter(dependencies=[Depends(get_current_actor)])
api_router.include_router(access_router)
api_router.include_router(entity_schemas_router)
api_router.include_router(metadata_router)
api_router.include_router(excel_router)
api_router.include_router(dictionaries_router)
api_router.include_router(entity_objects_router)
api_router.include_router(relations_router)
api_router.include_router(attachments_router)
api_router.include_router(change_sets_router)
api_router.include_router(imports_router)
api_router.include_router(generated_api_router)
api_router.include_router(notifications_router)
api_router.include_router(users_router)
api_router.include_router(audit_router)
api_router.include_router(search_router)
api_router.include_router(geocoding_router)
api_router.include_router(workflows_router)
