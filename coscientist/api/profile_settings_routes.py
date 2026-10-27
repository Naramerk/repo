"""Local preference control routes, composed with a trusted runtime catalog."""
from fastapi import FastAPI, HTTPException
from pydantic import Field

from coscientist.api.local_profiles import LocalProfiles, ProfileRefuse
from coscientist.api.profile_settings import CatalogProvider, ProfileSettings, SettingsModel, SettingsValues


class SaveSettingsBody(SettingsModel):
    expected_revision: int = Field(ge=0)
    catalog_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    settings: SettingsValues


def register_profile_settings_routes(app: FastAPI, *, profiles: LocalProfiles,
                                    catalog: CatalogProvider | None) -> None:
    store = ProfileSettings(profiles)

    @app.get("/console/settings-catalog")
    def settings_catalog() -> dict[str, object]:
        if catalog is None:
            raise HTTPException(status_code=503, detail="settings_catalog_not_configured")
        try:
            return catalog().model_dump(mode="json")
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    @app.get("/console/profiles/{profile_id}/settings")
    def profile_settings(profile_id: str) -> dict[str, object]:
        try:
            return store.get(profile_id)
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc

    @app.put("/console/profiles/{profile_id}/settings")
    def save_profile_settings(profile_id: str, body: SaveSettingsBody) -> dict[str, object]:
        if catalog is None:
            raise HTTPException(status_code=503, detail="settings_catalog_not_configured")
        try:
            return store.save(profile_id, expected_revision=body.expected_revision, catalog_digest=body.catalog_digest,
                              settings=body.settings, catalog=catalog())
        except ProfileRefuse as exc:
            raise HTTPException(status_code=exc.status, detail=exc.reason) from exc
