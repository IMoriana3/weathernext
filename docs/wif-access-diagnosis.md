# WeatherNext 3 — diagnóstico del 403 WIF y plan de automatización

Fecha: 2026-10-21. Base evaluada: `main` @ `fc0d8dc077eeb6f8bf3ebdbc70149a95d46ecde9`.
Etiquetas: HECHO / INFERENCIA / PROPUESTA / DECISIÓN. Divergencias: INTENCIONAL /
APROXIMACIÓN / LEGACY / BUG / UNKNOWN. Ejecución: PASSED / FAILED / SKIPPED /
NOT_RUN / ENVIRONMENT_BLOCKED.

## A. Diagnóstico del HTTP 403

| # | Clase | Afirmación | Evidencia |
|---|---|---|---|
| 1 | HECHO | La federación GitHub OIDC → STS → `generateAccessToken` funciona. | Run [39668940984](https://github.com/IMoriana3/weathernext/actions/runs/39668940984): `gcloud auth print-access-token` PASSED. |
| 2 | HECHO | El bucket `weathernext3_statistics_spatial` existe. | Petición anónima a `storage/v1/b/weathernext3_statistics_spatial` → **401**; un nombre inexistente → **404** (21-10-2026, desde este entorno). |
| 3 | HECHO | Google documenta el acceso por *allowlist* de la **dirección de correo de cuenta Google** enviada en el *WeatherNext Data Request Form*; una aprobación cubre Cloud Storage, BigQuery y Earth Engine. No documenta altas de cuentas de servicio, grupos ni principales WIF. | Guía «Quick start: Accessing WeatherNext forecasts» (`developers.google.com/weathernext/guides/access-forecast`) y página de soporte; contacto: `weathernext@google.com`. |
| 4 | HECHO | El acceso se materializa como un binding IAM sobre la cadena enviada en el formulario. | [google-deepmind/weathernext#226](https://github.com/google-deepmind/weathernext/issues/226): un usuario aprobado recibe 403 por un carácter invisible en su email; cerrado tras reenviar el formulario. |
| 5 | HECHO (confirmado por 7) | El 403 se debe a que `wn3-ingest@auspicious-silo-445105-d4.iam.gserviceaccount.com` **no está en la allowlist** del bucket, no a la retirada del objeto. | En GCS, un llamante **con** `storage.objects.get` sobre un objeto inexistente recibe **404**; el 403 indica falta de permiso. Un bloqueo VPC-SC o *requester pays* devuelve otros mensajes (`vpcServiceControlsUniqueIdentifier` / `UserProjectMissing`). El bucket de estadísticas no es *requester pays*. |
| 6 | HECHO | Nada que hagamos en `auspicious-silo-445105-d4` concede acceso: la política IAM del bucket pertenece al proyecto del proveedor. | Modelo IAM de GCS. |
| 7 | HECHO | `20261019_06hr_01_preds/predictions.zarr/zarr.json` **sigue disponible** para la cuenta humana aprobada: `gcloud storage ls` sobre esa ruta exacta devuelve el objeto (Cloud Shell, operador, 2026-10-21). Con el punto 5, el 403 de la cuenta de servicio es **falta de autorización del proveedor**, no retirada del objeto. |

### Segundo bloqueo encontrado: BUG en el lector (corregido en este PR)

| Clase | Afirmación |
|---|---|
| HECHO / **BUG** | `obstore==0.12.0` (`GCSStore`) **no puede leer el ADC `external_account`** que escribe `google-github-actions/auth`: `Unable to decode service account file: unknown variant 'external_account', expected 'service_account' or 'authorized_user'`. Reproducido localmente con un ADC WIF de prueba. Aunque Google añadiera la cuenta de servicio, `scheduled_ingest.py` habría fallado con `INGEST_FAILED: GenericError`. |
| HECHO | `auth-check` sólo probaba `gcloud`, que sí admite WIF; por eso no detectaba el fallo. `docs/automated-ingestion.md` lo tenía como UNKNOWN. |
| HECHO | Corrección: `point_reader._gcs_auth_options()` delega **sólo** el caso `external_account` en `google.auth.default(scopes=[devstorage.read_only])` + `obstore.auth.google.GoogleCredentialProvider`. Cloud Shell (`authorized_user`/metadata) y claves de cuenta de servicio siguen la ruta nativa sin cambios. Con un ADC WIF de prueba, la petición ya llega a Google STS (rechazada por token ficticio: `invalid_grant`), en lugar de fallar al decodificar. |
| HECHO | `auth-check` ahora también: (a) lista un prefijo fechado (`storage.objects.list`, que necesita el descubrimiento) y (b) lee `zarr.json` **con el mismo cliente obstore** que usa la ingesta (`python -m scripts.probe_metadata`). No lee valores de pronóstico ni toca Supabase. |

## B. Solución recomendada

1. **Operador — solicitar el alta de la cuenta de servicio** (único camino que no depende de credenciales personales). Reenviar el formulario de WeatherNext con la cuenta de servicio como identidad y, en paralelo, escribir a `weathernext@google.com` (borrador abajo). Copiar el email **a mano**, sin caracteres invisibles (ver #226).
2. Mientras tanto, **validar la hipótesis** con la cuenta humana en Cloud Shell (punto A.7) y comprobar que el email humano aprobado ve el objeto.
3. Al recibir confirmación: fusionar este PR y lanzar `ingest.yml` con `mode=auth-check`. Deben aparecer `WN3_GCP_AUTH_AND_LIST_OK` y `WN3_READER_ZARR_V3_METADATA_OK`.
4. Si Google **no** admite cuentas de servicio, alternativas por orden de preferencia:
   - Preguntar si aceptan un **Google Group** propio (añadiríamos la SA al grupo) — mínimo privilegio y rotación sin re-solicitar.
   - Preguntar por el acceso equivalente en **BigQuery Analytics Hub** (la misma aprobación lo cubre; la suscripción crea un *linked dataset* en nuestro proyecto sobre el que sí controlamos IAM para la SA). Requiere rediseñar la lectura: PROPUESTA, no desarrollar sin decisión.
   - **No recomendado:** credencial `authorized_user` del humano guardada como secreto de GitHub (refresh token persistente con todos los permisos del usuario, sin caducidad efectiva, en un repo público). Sólo con aceptación explícita del riesgo.

Permisos que solicitamos al proveedor: `roles/storage.objectViewer` (o equivalente con `storage.objects.get` + `storage.objects.list`) sobre `gs://weathernext3_statistics_spatial` (y no sobre `weathernext3_spatial`).

### Solicitud a Google

- 2026-10-21: correo enviado a `weathernext@google.com` (texto abajo).
- 2026-10-22: el equipo WeatherNext responde que **cada cuenta adicional requiere su propio envío del Data Request Form**. HECHO: el alta es por identidad vía formulario.
- 2026-10-22: el operador envía el formulario para `wn3-ingest@auspicious-silo-445105-d4.iam.gserviceaccount.com`. Pendiente de aprobación; se comprobará con `auth-check`, no por correo.

Texto del correo enviado:

> **Asunto:** WeatherNext 3 Statistics — add service account to existing approval (GCS read)
>
> Hello WeatherNext team,
>
> Our organization already has approved WeatherNext access for a human Google account (the one used on the original Data Request Form). We are automating a daily, bounded point-forecast read for our solar plants and would like the same read access granted to a dedicated service account, used only via Workload Identity Federation (no long-lived keys):
>
> - Principal: `serviceAccount:wn3-ingest@auspicious-silo-445105-d4.iam.gserviceaccount.com`
> - GCP project: `auspicious-silo-445105-d4` (number `1056540928084`)
> - Bucket: `gs://weathernext3_statistics_spatial` only (statistics, not the 64-member ensemble bucket)
> - Permissions needed: `storage.objects.get` and `storage.objects.list` (read-only)
> - Usage: ≤ 4 runs/day, one 0.1° grid cell, 12 fields, ≤ 360 h per run
>
> Evidence: the service account authenticates successfully, but `gcloud storage ls` on `.../20261019_06hr_01_preds/predictions.zarr/zarr.json` returns `403 storage.objects.get denied` (GitHub Actions run 39668940984, 2026-10-21).
>
> If service accounts cannot be allowlisted, could you tell us the supported identity type for unattended access (e.g., a Google Group, or the BigQuery Analytics Hub listing)?
>
> Thank you,
> [nombre / email de la cuenta aprobada]

## C. Código

PR de esta rama: puente WIF → obstore, sonda del lector en `auth-check`, 5 tests offline nuevos (122 en total, PASSED en local con Python 3.13; CI con 3.12). Sin cambios en tablas, contratos, filas ni en la puerta `WN3_INGESTION_APPROVED`.

## D. Plan de validación de la primera ingesta real

| Paso | Acción | Criterio de aceptación |
|---|---|---|
| 0 | Branch protection/ruleset y Environment (ver F) | Configurado antes de cualquier `publish`. |
| 1 | `auth-check` | 3 sondas OK (token, list+get gcloud, `zarr.json` vía obstore). |
| 2 | Ensayo sin escritura: ejecutar `scheduled_ingest.py` **sin** `--publish` en un `workflow_dispatch` (requiere añadir un modo `dry-run` al workflow; PROPUESTA, no incluido aquí para no ampliar el PR) | `VALIDATED_NOT_PUBLISHED: rows=24`; duración y nº de peticiones GCS registradas. |
| 3 | `WN3_INGESTION_APPROVED=true` + `mode=publish`, `hours=24` | `VERIFIED_AFTER_WRITE: rows=24 newly_inserted=24`; filas del ciclo 20261019_06 intactas (consulta por `init_time`, 360 filas, mismos valores). |
| 4 | Repetir el mismo ciclo | `EXISTING_RUN_VERIFIED_NO_FETCH` (0 lecturas GCS). |
| 5 | Siguiente ejecución programada | Nuevo `init_time`, 24 filas, sin mezclar inicializaciones. |

## E. Arquitectura 24/72/360 h

- **Selección de ciclo (APROXIMACIÓN):** con el cron 06:17/18:17 y la madurez mínima de 9 h, sólo se seleccionan los ciclos **18Z y 06Z**; 00Z y 12Z nunca se ingieren. Si se quieren los cuatro, cron cada 6 h a `:17` desplazado (p. ej. `17 3,9,15,21 * * *` → 18Z, 00Z, 06Z, 12Z con ~9 h de margen). DECISIÓN pendiente de coste.
- **Checkpoints persistentes:** el repo es **público**: no usar caché/artefactos de Actions para datos licenciados (las cachés de `main` son restaurables desde ramas). PROPUESTA: bucket privado propio `gs://<proyecto>-wn3-checkpoints` (región `europe-southwest1`), *uniform access*, *public access prevention*, *lifecycle delete* a 3 días, y a la SA sólo `roles/storage.objectUser` sobre ese bucket con condición de prefijo `planta_<id>/<init>/`. Clave de checkpoint = (planta, celda, `run_prefix`, campo, ventana de leads), validada contra `init_time` antes de reutilizar. Coste: KB por ciclo → céntimos/mes (infra de pago: requiere autorización).
- **Escalado de horizonte:** 24 h en cada ciclo; 72 h y 360 h sólo para 00Z (o un ciclo/día), reanudando desde checkpoint persistente; el timeout de 150 min se mantiene como techo.
- **Varias plantas:** una matriz por planta con `concurrency` por planta (`wn3-<planta_id>`); la celda y la identidad se verifican ya por fila. Agrupar plantas que compartan celda 0.1° para no duplicar lecturas.
- **Observabilidad:** (a) fallo del job → notificación de GitHub + issue automática; (b) *freshness* en Supabase: alerta si `max(init_time)` por planta > 30 h; (c) registrar duración y nº de leads leídos (sin valores).
- **SolarGPT:** el consumo pasa por WeatherContract v2 en `IMoriana3/SolarGPTfull` (`solargpt/solargpt_core/weather_contract.py`). `surface_solar_radiation_downwards_1hr` (J m⁻²) no es GHI instantánea; ni DNI/DHI ni STOW salen de aquí.

## F. Quién hace qué

**Requiere al operador** (permisos, cuentas o pagos que no controlo):

1. Enviar formulario/correo a Google (borrador arriba) y compartir la respuesta.
2. Comprobar en Cloud Shell, con la cuenta humana, el objeto de A.7.
3. Ruleset en `main` (Settings → Rules): exigir PR, check `offline-tests`, bloquear *force-push* y borrado. Hoy `main` no está protegida y cualquier push a `main` puede obtener el token de la SA y el secreto de Supabase.
4. GitHub Environment `wn3-production` restringido a `main` con revisor obligatorio; mover `WN3_SUPABASE_SECRET_KEY` allí.
5. ~~Endurecer la condición del proveedor WIF~~ — HECHO (verificada por el operador, 2026-10-21): `attribute.repository_id=='1425584477' && attribute.repository_owner_id=='168933412' && attribute.ref=='refs/heads/main' && attribute.workflow_ref=='IMoriana3/weathernext/.github/workflows/ingest.yml@refs/heads/main'`. Correcta y mínima; sin cambios. Si se añade el Environment, se podrá sumar `assertion.environment=='wn3-production'`.
6. Autorizar (o no) el bucket de checkpoints y activar `WN3_INGESTION_APPROVED`.

**Puedo completar vía GitHub:** este PR (fix + sonda + tests + doc), el modo `dry-run` del workflow, el job de *freshness*/alertas, la matriz multi-planta y el diseño de checkpoints persistentes, siempre por PR y sin activar publicación.
