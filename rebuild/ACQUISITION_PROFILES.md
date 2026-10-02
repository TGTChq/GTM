# Adquisición sin lista cerrada de títulos — 2026-09-16

## Decisión

No usar `title`, `title_advanced`, palabras obligatorias en descripciones, ni
`remote-only` para decidir qué comprar. El objetivo es obtener más vacantes
únicas compatibles por crédito, no mejorar un porcentaje ocultando vacantes.

Dos perfiles versionados con ventanas y cursores independientes:

| Perfil | Consulta a Fantastic | Propósito |
| --- | --- | --- |
| priority_v1 | Mercado actual, exclusiones sectoriales actuales, FULL_TIME, headcount 25–1000 y coincidencia con **cualquiera** de 16 familias amplias | Priorizar inventario potencialmente útil sin depender del nombre del puesto |
| discovery_v1 | Mismo mercado y exclusiones sectoriales; sin filtros de título, función, tamaño, jornada ni modalidad | Recuperar datos faltantes y errores de etiquetas de la vía prioritaria |
| legacy_v1 | Consulta histórica, congelada al primer intento contabilizable | Conservar el estado anterior sin reescribir cursores |

Las familias incluyen Management & Leadership, Retail y Logistics por su
solapamiento con product/operations/ecommerce; no equivalen a aceptar jefaturas,
trabajo presencial físico ni trabajo de almacén. La elegibilidad sigue pasando
por responsabilidades, evidencia del empleador y reglas del sistema.

La exploración conserva la geografía y las exclusiones sectoriales acordadas:
no representa todo el mercado mundial ni recupera errores de esos dos filtros.

## Contratos comprobados en documentación oficial

- La API documenta `ai_taxonomies_a` como coincidencia con cualquier categoría;
  `_primary` solo mira la primera. `exclude_ai_taxonomies_a` elimina un puesto
  aunque la categoría excluida sea secundaria. El tamaño usa límite superior
  exclusivo: 1001 conserva 1000. FULL_TIME admite etiquetas mixtas como
  FULL_TIME+CONTRACTOR; no garantiza contrato indefinido.
  [Referencia](https://developer.fantastic.jobs/api/new-jobs).
- La extracción y los enlaces de empresa pueden equivocarse. No usar etiquetas
  ausentes como rechazo definitivo. [Enriquecimientos](https://developer.fantastic.jobs/documentation/enrichments).
- Los Jobs credits se cobran por resultados devueltos. Deduplicar localmente no
  evita que el proveedor cobre otra observación. Ahora se registra el importe
  exacto de `x-api-jobs-this-request`; un saldo restante no prueba ese importe.
  [Consumo](https://developer.fantastic.jobs/documentation/credit-usage).
- Se incluye un constructor puro `count_query` para ambos endpoints de conteo.
  Quita los parámetros de paginación/formato no admitidos. No ejecuta la llamada.
  Los counts cuestan una petición, no Jobs credits, y no son necesarios para
  paginar. [Counts](https://developer.fantastic.jobs/documentation/endpoints/count-endpoints).

## Costes, cobertura y duplicados

`balanced_v1` reparte los slots previstos en 80% prioridad y 20% exploración,
intercalando ATS y JB. Es una proporción de slots, **no** de dinero ni de filas.
La exploración aparece en el tercer slot. Por defecto hay 10 slots globales por
ciclo, una página por slot. Las llamadas reales pueden ser menos por ventanas
vacías, límites o errores. No se transfiere capacidad entre perfiles en silencio.

Ambos perfiles requieren y comparten el presupuesto persistente existente. Las
reservas se hacen antes de llamar, los reintentos físicos internos se desactivan,
y reiniciar el proceso no devuelve créditos al presupuesto. No se crea ni se
amplía ningún presupuesto con este cambio. Todos los rechazos de autenticación,
cuota, errores de request y timeouts detienen las compras de ese ciclo.

La exploración puede volver a traer un job prioritario: queda en el recibo como
gasto, pero no crea otro posting ni reabre un rechazo sin cambios de evidencia.
Los cursores amplios avanzan de la ventana más antigua a la nueva. Un presupuesto
insuficiente puede dejar backlog; no se afirma cobertura total. Ventanas que ya
no cubre el feed quedan `failed`, nunca falsamente `complete`.

El ledger agrega `acquisition_profiles`: peticiones, créditos estimados y
confirmados conocidos, vacantes únicas atribuidas a su primer recibo, vacantes
actualmente compatibles, contactos aprobados y ventanas incompletas. No es un
experimento A/B aleatorio ni prueba de recall: son conjuntos solapados y la
atribución es al primer recibo. `complete_coverage` significa consulta guardada
completa, no mercado completo. Los importes confirmados pueden ser parciales si
faltan cabeceras; los timeouts siguen reservados como inciertos.

## Contraste offline con los 50 registros del usuario

El simulador de los filtros documentados obtiene 7 candidatos prioritarios y
24 candidatos de exploración, 17 solo por exploración. **No son 7/24 leads**.
El Marketing Analyst sin headcount solo entra por exploración. Entre los 7
prioritarios siguen apareciendo trabajos físicos, autorizaciones de seguridad
y un voluntariado mal etiquetado. La clasificación posterior sigue siendo
indispensable. Los estados anteriores del export NO son una verdad de referencia.

Reproducir, sin claves, red ni SQL:

```powershell
python -m rebuild.audit_acquisition_profiles --cohort "C:\ruta\GTM-cohorte-50-20260916-083740.json"
```

## Aplicación segura

1. Ejecutar el paquete entregado primero para validar y publicar. Reutiliza el
   validador de release: checkout aislado, legacy completa, core completa con
   PostgreSQL temporal, y CI Linux del SHA exacto antes de promover feat/rebuild-core.
   No permite omisiones de core. La publicación no invoca Railway, pero un
   auto-deploy que ya esté configurado puede reaccionar al push.
2. Desplegar el SHA validado con autorun/entrega detenidos. Se requiere migración
   005 antes de ejecutar; `python -m tgtc_core migrate` es el comando existente.
   No borrar tablas ni reiniciar particiones. La migración es transaccional y
   conserva IDs, recibos, offsets, presupuestos y estados.
3. Activar en el servicio/worker correcto:

```text
TGTC_ACQUISITION_STRATEGY=balanced_v1
TGTC_FANTASTIC_CYCLE_PAGE_SLOTS=10
TGTC_ACCEPTANCE_MODE=bounded
```

   No añadir claves nuevas. Requiere `TGTC_SPEND_BUDGET_ID` de un presupuesto
   válido ya autorizado; ausencia o agotamiento impide compras. No recargar un
   presupuesto viejo reutilizando su nombre. Mantener los límites de páginas,
   proveedores e inferencia existentes. Para la primera prueba usar `--no-deliver`.
4. Validar recibos reales, jobs compatibles únicos/crédito, atribución de empresa,
   contactos aprobados y backlog por perfil. No escalar basándose solo en el
   número menor de descartes. La llamada real al proveedor NO se realizó aquí.

El valor por defecto es `legacy_v1`: publicar el código no cambia silenciosamente
el alcance de producción. Cambiarlo a `legacy_v1` con **este código nuevo** permite
retomar los cursores anteriores. No hacer rollback a binarios anteriores sobre
schema 005: su antiguo ON CONFLICT no conoce la nueva clave de partición.

## Evidencia y límites de esta entrega

El paquete incluye un resumen honesto de las pruebas ejecutadas localmente.
El entorno de preparación no permite crear el usuario no-root de PostgreSQL;
las pruebas integradas quedan obligatoriamente pendientes del validador Windows
y CI Linux. No se simuló una aprobación de esos gates ni se hicieron llamadas
pagadas. Una prueba offline no acredita conversión real ni óptimo global.
