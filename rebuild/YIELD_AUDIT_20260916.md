# Auditoría de las 50 vacantes — 16 septiembre 2026

Estado: correcciones locales en validación; **no desplegado ni certificado para producción**.
Base Git: `8791e3d1e78485ab72ca2b468aeea6b96c32272f`.
Fuente: exportación de solo lectura proporcionada por Luis, 08:37 UTC.
SHA-256: `ab3feab645a031ae67fad1dbf33509f923ffc4d20a164554c80cc1178d08469b`.

## Conclusión

El cuello de botella principal es la adquisición: **39/50 vacantes (78%)** pertenecían,
según los datos del proveedor, a empresas fuera del rango o de la industria admitidos.
No eran 50 oportunidades comerciales calificadas. La cohorte pequeña no demuestra la
tasa de conversión futura ni una optimización máxima.

Las 45 vacantes rechazadas tienen motivos sustentables bajo la política existente,
pero varias explicaciones del clasificador eran incorrectas. No hay evidencia de
45 falsos negativos; tampoco deben tratarse los motivos originales como verdad.
Esta auditoría contrasta los anuncios y la política; no verifica independientemente
todos los tamaños/industrias que devuelve el proveedor.

La única oportunidad registrada como aprobada tiene una discrepancia de empleador:
la vacante de contabilidad corresponde a **Volunteer Energy Cooperative**, no a
Tennessee Valley Public Power Association. El [Jobline oficial de TVPPA](https://www.tvppa.com/jobline)
lo confirma, y también [el anuncio oficial completo](https://www.tvppa.com/job-listings/junior-accountant-accounts-payable-specialist).
El contenido contiene el nombre del empleador y el dominio de solicitud
vec.org. El JSON no incluye recibos de entrega: **no permite afirmar si ese aprobado
ya fue enviado**. Antes de reactivar entrega debe revisarse/revocarse la aprobación
incorrecta y reconstruirse la oportunidad con identidad y datos del empleador real.

## Recuento reproducible

| Medida | Resultado |
|---|---:|
| Vacantes de entrada | 50 |
| Rechazadas por clasificación original | 45 |
| Vacantes clasificadas compatibles originalmente | 5 |
| Oportunidades creadas (una vacante puede generar dos funciones) | 6 |
| Fuera por tamaño conocido (<25 o >1.000) | 32 |
| Fuera por industria con alias nonprofit normalizado | 26 |
| Unión tamaño/industria; sin contar dos veces | 39 |
| Vacantes que superan esos dos filtros con los datos actuales | 11 |
| Conflictos de empleador corroborados en el texto | 1 |
| Compatibles originales pero excluidas luego por empresa | 2: IDs 38 y 46 |
| Compatible original que era voluntariado | 1: ID 34 |
| Vacante compatible con búsqueda sin resultados | 1: ID 8 |
| Vacante potencialmente compatible con empleador por reparar | 1: ID 28 |

Antes de normalizar el alias “Non-profit Organization Management” se contaban 25 por
industria. Su corrección añade Wild Virginia, ya fuera por tamaño: la unión sigue en 39.

## Por qué se descarta cada una

“Empresa fuera” significa la política vigente, no que la empresa o el puesto carezcan
de valor comercial en general. Cambiar esa política requeriría medir otro mercado,
no maquillar la conversión del mismo experimento.

| ID | Vacante | Empresa recibida | Empleados recibidos | Revisión y evidencia |
|---:|---|---|---:|---|
| 1 | Patient Registrar | HCA Healthcare | 154889 | Registro de pacientes al pie de cama; además industria/tamaño. |
| 2 | Medical Laboratory Technician Nights | HCA Houston Healthcare | 4040 | Muestras y pruebas de laboratorio clínico; además industria/tamaño. |
| 3 | Director, Customer Intelligence & Performance | Moody's Corporation | 15159 | Director; empresa fuera de tamaño. |
| 4 | Vice President Anesthesia Operations | HCA Healthcare | 154889 | VP; además industria/tamaño. |
| 5 | Fire Engineering Intern (Available June 2026) | Arup | 26800 | Prácticas. La etiqueta anterior de liderazgo era incorrecta; además tamaño. |
| 6 | Marketing Intern | Jet Set Travel co | desconocido | Prácticas de marketing, parcial/sin pago. |
| 7 | RN Clinical Staff Emergency Room Full Time Evenings 3pm - $14 Differential | MLK Community Healthcare | 918 | Enfermería de urgencias presencial; además industria. |
| 8 | Marketing Analyst | Courtship | desconocido | Vacante compatible de marketing. Sin candidatos encontrados en la búsqueda ejecutada; NO prueba de ausencia absoluta en Apollo. |
| 9 | Water Technology Technician | Cape Environmental Management Inc | 236 | Preparación de materiales, ensamblaje y expedición; trabajo físico. |
| 10 | Data Visualization Designer (Communications Specialist) | StratasCorp Technologies | 204 | Exige autorización de seguridad activa. |
| 11 | Customer Service Representative (Remote) | Sight Sea Travel | 2 | Parcial y empresa de 2 empleados. |
| 12 | Senior Helpdesk Specialist | Chenega MIOS SBU | 236 | Exige autorización de seguridad. |
| 13 | Operations Coordinator | Wild Virginia | 12 | Residencia en Virginia y viajes ocasionales NO bastan para descartarla. Sigue fuera por 12 empleados e industria nonprofit. |
| 14 | Production Assembler I | General Dynamics Ordnance and Tactical Systems | 2001 | Ensamblaje físico; además tamaño. |
| 15 | Kids Club Team Member | Chuze Fitness | 969 | Parcial y cuidado presencial infantil. |
| 16 | Product Operations Lead / King of Prussia | lululemon | 25339 | Dirección de personas; además tamaño. |
| 17 | Guest Experience Lead / Regency Mall | lululemon | 25339 | Atención/operación de tienda física; además tamaño. |
| 18 | EDUCATOR / STEAMBOAT SPRINGS | lululemon | 25339 | Parcial y trabajo de tienda. EMEA-only aparece en un aviso legal, no acredita mercado extranjero; además tamaño. |
| 19 | Esthetician Job Cincinnati Ohio | SalonRenter Inc. | 2 | Parcial; empresa de 2 empleados. |
| 20 | Data Engineering Lead - Senior Vice President | iCapital | 2418 | SVP; además tamaño. |
| 21 | Compliance Support Specialist | Aristocrat | 6896 | Licencia de juego obligatoria, viajes hasta 25%; además tamaño. |
| 22 | Doyle Security Services, Inc. | Doyle Security Services, Inc. (DSS) | 416 | Patrullaje presencial de seguridad. |
| 23 | Public Safety Armed | Yale New Haven Health | 5853 | Parcial, seguridad armada; además industria/tamaño. |
| 24 | Product Management Co-Op | Johnson & Johnson MedTech | 38093 | Programa co-op; además industria/tamaño. |
| 25 | CDL Delivery Driver | Nutrien | 7053 | Conducción CDL y trabajo físico; además tamaño. |
| 26 | Staff Software Engineer - Ingestion Platform | Reddit, Inc. | 5126 | NO es contrato: la frase venía del aviso de privacidad. Sigue fuera por Staff Engineer y 5.126 empleados. |
| 27 | Registered Nurse, RN - Hospice - PRN | VitalCaring Group | 700 | Enfermería de campo/hospicio; además industria. |
| 28 | Junior Accountant- Accounts Payable Specialist | Tennessee Valley Public Power Association, Inc. | 33 | ERROR DE EMPLEADOR: el anuncio identifica Volunteer Energy Cooperative / vec.org. TVPPA es el publicador. El aprobado actual no es defendible; no transferirle sus 33 empleados ni su comprador. |
| 29 | Human Resources Coordinator | Gusmer Enterprises | 196 | Puesto mixto: recepción de visitantes, correo y suministros. Levantar ocasionalmente 20 lb no basta; el nuevo motivo físico usa la responsabilidad de recepción explícita. |
| 30 | Accounting Intern | Lafayette 148 New York | 383 | Prácticas/parcial de contabilidad. |
| 31 | Medical Assist. Phy. Pract Sup | Butler | 698 | Atención clínica: signos vitales, inyecciones y extracciones; además industria. |
| 32 | Sr. Staff Backend Engineer | Coupang | 9575 | Sr Staff; además tamaño. |
| 33 | Medical Office Specialist | HCA Healthcare | 154889 | Recepción de pacientes, caja y registro; además industria/tamaño. |
| 34 | Legal & Governance Advisor (Volunteer) | IBSS | 340 | FALSO POSITIVO: asesoría voluntaria 4–8 h/mes; fue clasificada people_hr por texto genérico. No es una vacante de contratación full-time. |
| 35 | Patient Monitor Tech | HCA Florida Orange Park Hospital | 467 | Trabajo clínico presencial de hospital; además industria. |
| 36 | Pharmacist Seven on Seven off | Mission Health | 3123 | Farmacia clínica; además industria/tamaño. |
| 37 | Pharmacy Technician | Reston Hospital | 717 | Preparación/dispensación de medicamentos; además industria. |
| 38 | Scheduling Assistant | HCA Healthcare | 154889 | Funciones administrativas compatibles, pero HCA fuera por industria/tamaño. Sus dos oportunidades no equivalen a dos vacantes. |
| 39 | Medical Assistant | HCA Healthcare | 154889 | Atención clínica presencial; además industria/tamaño. |
| 40 | Emergency Room Resource Pool Nurse | TriStar Centennial Medical Center | 1221 | Enfermería de urgencias; además industria/tamaño. |
| 41 | Clinical Nurse Coordinator ER | TriStar Centennial Medical Center | 1221 | Coordinación/supervisión clínica; además industria/tamaño. |
| 42 | Night Housekeeper | Mission Health | 3123 | Limpieza de hospital; además industria/tamaño. |
| 43 | Perinatal Nurse Navigator | HCA Healthcare | 154889 | Enfermería clínica y licencia obligatoria; además industria/tamaño. |
| 44 | Medical Assistant | HCA Healthcare | 154889 | Signos vitales, inyecciones, extracciones; además industria/tamaño. |
| 45 | RN LDRP Nights | Lakeview Hospital | 572 | Enfermería presencial; además industria. |
| 46 | Senior UX Designer | HCA Healthcare | 154889 | Diseño/UX compatible por función, pero HCA fuera por industria/tamaño. |
| 47 | Telephone Triage Virtual RN | HCA Healthcare | 154889 | Es VIRTUAL: la experiencia previa al pie de cama no implica trabajo presencial. Licencia obligatoria NV/CA/AK; además industria/tamaño. |
| 48 | EVS Tech II Evenings | Medical City Healthcare | 7389 | Limpieza/servicios físicos de hospital; además industria/tamaño. |
| 49 | Critical Care Intensivist | HCA Florida Brandon Hospital | 440 | Médico intensivista clínico; además industria. |
| 50 | Maintenance Technician II | Medical City Healthcare | 7389 | Mantenimiento físico de instalaciones hospitalarias; además industria/tamaño. |

## Cambios implementados

1. **Identidad:** dos anclas textuales concordantes (empleador explícito y dominio
   de solicitud) detectan conflictos. Se frena identidad, cualificación, aprobación
   y entrega. No se fusionan empresas ni se traslada un comprador mediante texto.
   El bloqueo también comprueba aprobaciones antiguas justo antes de enviarlas.
2. **Calidad de decisiones:** las exclusiones del modelo requieren evidencia
   literal, confianza suficiente y corroboración contextual. Un rechazo no respaldado
   queda sin resolver; no pasa a aprobado. Se versiona el esquema/cache de inferencia.
3. **Regresiones reales:** corregidos contrato inferido de privacidad, voluntariado
   entre paréntesis, intern etiquetado como ejecutivo, licencia obligatoria y confusión
   entre experiencia clínica previa y trabajo físico actual, y región del aviso de
   privacidad confundida con mercado del puesto. Recepción explícita se
   distingue del levantamiento ligero incidental. Los extractos conservan la frase
   que activó la regla incluso en párrafos largos.
4. **Adquisición:** nuevas consultas excluyen agencias e industrias ya prohibidas
   por política mediante [parámetros oficiales de Fantastic](https://developer.fantastic.jobs/documentation/endpoints/new-jobs).
   Aplicados retrospectivamente a los campos exportados, los nombres de industria
   excluirían 26/50; **esto es una simulación, no un ahorro facturado observado**.
   No se fuerzan títulos, remoto, tamaño conocido ni FULL_TIME positivo: eliminarían
   casos permitidos con campos faltantes. FULL_TIME del proveedor usa coincidencia
   por solapamiento y no basta para excluir etiquetas mixtas.
   Las empresas con tamaño conocido fuera de política se excluyen mediante su slug
   exacto, con vigencia máxima de siete días y un máximo de cien empresas por consulta.
   No se excluyen empresas de tamaño desconocido, ni identidades en conflicto.
   La documentación no especifica completamente cómo los filtros negativos tratan
   valores de industria/agencia nulos. Las pruebas simuladas conservan desconocidos;
   falta comprobar ese comportamiento del proveedor antes de habilitar estos filtros
   en producción. La simulación no acredita recall real.
5. **Paginación:** cada partición reutiliza su consulta histórica, incluso si era
   sin filtros. Solo se modifican offset/limit. Un cursor sin historial o una ventana
   fuera del feed se detienen sin declararlos completos. Los nuevos filtros no se
   aplican a mitad de una partición antigua. Una ventana histórica aún no iniciada
   selecciona el feed de seis meses cuando ya no cabe en el de siete días, según la
   [estrategia documentada del proveedor](https://developer.fantastic.jobs/documentation/recommended-strategy).
6. **Apollo:** los selectores conocidos de dominio y organización aportan candidatos
   antes de gastar en enriquecimiento. Se deduplican y priorizan dentro del límite
   existente de páginas y de tres intentos pagados por época. Una empresa ya descartada
   no consume enriquecimiento para volver a descubrir su incompatibilidad.
   Si la búsqueda estricta no obtiene candidatos utilizables, se realiza una segunda
   búsqueda con títulos similares; permanecen todos los controles locales de cargo,
   empleo actual, país, supresión y correo. La [documentación de Apollo](https://docs.apollo.io/reference/people-api-search)
   indica que esta búsqueda no consume créditos de enriquecimiento; sí cuenta contra
   el presupuesto local de peticiones. Los recibos incluyen cobertura/paginación sin
   datos personales. Llegar al tope de páginas nunca se comunica como ausencia probada.
7. **Legacy:** el pre-rechazo por industria solo saltaba el enriquecimiento de empresa,
   pero todavía permitía búsqueda de contactos. Ahora produce un rechazo explícito antes
   de cualquier consulta de empresa o contacto; tampoco reutiliza un aprobado guardado
   contra ese veto. La prueba ya no captura excepciones genéricas que oculten fallos.
8. **Aislamiento y publicación:** el ejecutor de pruebas elimina credenciales y URLs
   de base de datos heredadas, bloquea HTTP antes del transporte/DNS y extiende la
   protección a subprocessos Python. Un intento HTTP externo hace fallar la batería
   aunque el código capture la excepción. La publicación exige baterías completas
   locales y jobs Linux test/core verdes para el SHA exacto, incluido arranque del
   contenedor sin red. Las pruebas portables no satisfacen esa puerta.

## Lo que estas correcciones NO demuestran ni hacen

- No certifican que todo candidato ausente de una búsqueda limitada no exista en Apollo.
- No verifican correos de PDL/Versium ni cambian proveedores.
- No amplían automáticamente industria, tamaño, geografías o criterios comerciales.
- No reabren en masa antiguos descartes ni borran su historia. El cambio de esquema
  no recupera por sí solo todos los descartes semánticos anteriores.
- No reparan por inferencia los datos de VEC: falta reconstruir esa identidad con
  evidencia del empleador correcto y validar sus contactos.
- No evitan absolutamente todo cobro duplicado de Fantastic. Se conservan IDs,
  recibos y cursores, y se evita reabrir un descarte idéntico; un timeout con resultado
  incierto, ventanas superpuestas o duplicados del proveedor pueden volver a facturarse.
- No certifican la precisión de todos los campos del proveedor ni la cobertura de
  todo Apollo. Siguen existiendo límites de páginas, presupuestos y requisitos de
  verificación de correo. La ampliación de títulos actúa sobre búsquedas sin candidatos
  utilizables; no promete que todo candidato contactable del proveedor fue evaluado.
- No se hicieron llamadas pagadas deliberadas, publicaciones remotas, SQL de producción
  ni entregas durante esta corrección. El intento HTTP de la prueba legacy fue bloqueado
  antes de transporte/DNS; se identificó y corrigió el flujo, y se repitió toda la suite
  con aislamiento reforzado. Esto no certifica retroactivamente posibles intentos de
  sesiones anteriores.

## Pruebas y puerta de salida

- 424 pruebas portables del core aprobadas, con red externa bloqueada.
- 157 pruebas que necesitan PostgreSQL aún NO ejecutadas en este entorno.
  Incluyen cinco regresiones nuevas de identidad, filtros/cursores y persistencia.
- 3.755 pruebas legacy y 1.001 subpruebas aprobadas; cero omisiones, cero intentos
  HTTP externos detectados en la ejecución final. JUnit reporta 4.756 resultados y
  3.755 casos; ambos recuentos se conservan por separado.
- Integridad del paquete: 35 comprobaciones, 0 diferencias y 0 ausentes.
- Lint: sin nombres indefinidos en los módulos cambiados. Comprobación de diferencias
  correcta. Dos avisos preexistentes de imports sin uso en una prueba legacy.
- Autoprueba del publicador: rechaza fallos, errores, omisiones, suites incompletas,
  conteos inflados por subpruebas, otro SHA y CI parcial. La evidencia portable real
  es rechazada deliberadamente como sustituto de los 581 casos completos del core.

La restricción local es de permisos del contenedor para iniciar PostgreSQL como
usuario no-root; no se modifica producción para sortearla. Además, el conector de
GitHub rechazó crear la rama de validación con `403 Resource not accessible by integration`.
Se entrega un publicador autocontenido para el acceso Git local que ya funciona.
El script crea un checkout separado, ejecuta ambas suites completas con PostgreSQL
temporal, publica primero una rama de validación y espera CI Linux. Solo tras ese
resultado promueve `feat/rebuild-core`, sin force. No despliega ni ejecuta proveedores.
Todavía se requiere validación controlada de llamadas reales y reparación de la
aprobación incorrecta antes de habilitar entrega. En la consulta de configuración de
Railway, CoreAcceptance seguía con arranque `describe`/`check-db` condicionado a modo
`read_only`; el conector no permitió ejecutar SQL de auditoría y no se creó otro servicio.

Siguiente orden seguro: validar las suites → publicar el cambio revisado → CI Linux
verde → comprobar que entrega siga detenida → sanear el aprobado de empresa errónea
→ recuperación acotada de registros existentes → prueba real con presupuesto y
recibos → ampliar adquisición. No volver a comprar la cohorte entera para probar
reglas que ya pueden evaluarse con este JSON.

## Reproducción local sin gasto

```powershell
python -m rebuild.audit_cohort "$env:USERPROFILE\Downloads\GTM-cohorte-50-20260916-083740.json"
```

El comando imprime hechos y diferencias deterministas; no simula una nueva llamada
a Claude ni convierte datos incompletos en aprobaciones. Para verificar el resultado
de inferencia nuevo debe usarse su esquema /2 y conservar sus recibos reales.
