# Agenda SaaS — Especificación de próximas features

**Documento de handoff.** Contexto, prioridades y límites para continuar el desarrollo.
Stack: Django + Django REST Framework, PostgreSQL, Docker. Multi-tenant.

---

## 0. REGLA CERO — No romper lo existente

**Este documento describe una evolución, no una reescritura.**

El sistema ya está construido, funcionando y con un cliente real en producción. La estructura de datos, la lógica de negocio y las convenciones actuales del proyecto son la referencia autoritativa: **este documento se subordina a ellas, no al revés.**

Reglas obligatorias:

1. **Auditar antes de proponer.** Es probable que varias de las funcionalidades descritas acá ya existan, total o parcialmente, resueltas de otra manera. Antes de escribir código, revisar el sistema e informar qué ya está cubierto, qué está cubierto parcialmente y qué falta realmente.
2. **Si ya existe con otro nombre o con otro enfoque, se respeta el existente.** No renombrar modelos, no reorganizar apps, no "normalizar" nomenclatura por preferencia estética.
3. **No cambiar contratos de API, modelos ni firmas existentes** sin señalarlo explícitamente y esperar aprobación.
4. **Nada de migraciones destructivas.** Cambios aditivos por defecto: campos nuevos nullable o con default, tablas nuevas antes que alterar existentes.
5. **Adaptarse a los patrones del repo** (organización de apps, separación selectors/services, un modelo por archivo, estilo de tests). Si algo de esta spec choca con esos patrones, gana el repo.
6. **Cambios incrementales y revisables.** Preferir varios PRs pequeños y acotados sobre un refactor grande.
7. **Ante conflicto entre esta spec y el código actual: señalarlo y proponer alternativa. No resolverlo en silencio.**

---

## 1. Contexto del producto

- Plataforma de agendamiento **multi-tenant**, base ya construida y en uso.
- Primer cliente real: estudio de pilates. Pipeline actual: salones de belleza, odontólogos, consultorios de salud independientes.
- **Alcance actual: agenda INTERNA.** El sistema lo opera el negocio (recepción, profesionales, dueño). **No hay interacción del cliente final con el sistema.** Todo lo que requiera que el cliente reserve, confirme o pague por sí mismo es **fase futura** (§6).
- El modelo **ya soporta citas no-1:1 / grupales**. No hace falta refactor de capacidad; verificar y, si acaso, completar bordes.
- **No se manejan monedas ni pagos** en esta etapa. Sin multi-moneda, sin señas, sin ledger de créditos.
- Segmento objetivo: negocios de **2 a 10 profesionales**.

### Decisión estratégica: motor genérico, no vertical

No se elige un rubro. Se construye un motor genérico + plantillas de configuración por rubro.

> Lo que parece diferencia de vertical casi siempre es diferencia de **vocabulario, capacidad y campos** → se resuelve con **datos y configuración**, no con ramificación de lógica.

**Regla dura:** no debe existir lógica de negocio ramificada por rubro (`if tenant.tipo == 'dental'`). La solución correcta es un capability flag, un campo configurable o seed data.

### Criterio de encaje del producto
Cliente válido: vende tiempo de una persona y/o un recurso, en bloques, con cita previa, y pierde ingreso si el bloque queda vacío.

**Fuera de alcance permanente** (romperían el modelo): órdenes de trabajo multi-día (taller mecánico), mesas y rotación (restaurantes), facturación regulada y obras sociales, servicios a domicilio con ruteo.

---

## 2. Prioridad ALTA — Robustez del motor

### 2.1 Concurrencia al agendar
Dos operadores del mismo negocio cargando el mismo bloque en simultáneo es el bug crítico de una agenda interna.

- Verificar primero cómo está resuelto hoy. Si ya hay locks o constraints, dejarlo como está.
- Si falta: lock a nivel DB al confirmar (`select_for_update` sobre profesional/recurso/bloque) + constraint de unicidad en DB como última defensa, no solo validación en Python.
- Endpoint de creación idempotente para tolerar doble submit.
- Tests de concurrencia explícitos.

### 2.2 Reglas de disponibilidad componibles
Probablemente exista parte de esto. Completar lo que falte:

- Buffers antes/después, configurables por servicio.
- Duración variable por servicio y por profesional.
- Horarios de excepción, feriados, bloqueos puntuales.
- Servicios habilitados por profesional.

### 2.3 Recursos, además de personas
Reformer, sillón, sillón dental, sala, box.

- Modelo de recursos por tenant, con cantidad.
- Un servicio declara qué recursos consume.
- El cálculo de disponibilidad intersecta disponibilidad de profesional **y** de recurso.

Es lo que hace que el mismo motor sirva a pilates, salón y consultorio sin bifurcarse.

### 2.4 Series de citas recurrentes
"Todos los lunes y miércoles a las 7" (pilates), control cada 6 meses (odontología), raíz cada 3 semanas (color).

- Serie con generación de ocurrencias.
- Manejo de excepciones: una ocurrencia cancelada o movida **no rompe la serie**.
- Edición "solo esta" vs "esta y las siguientes".

### 2.5 Zonas horarias
Paraguay y Brasil manejan horario de verano de forma distinta, y el mercado es fronterizo.

- Almacenar en UTC con timezone del tenant explícita.
- Testear cruces de DST en series recurrentes (el caso donde más se rompe).

---

## 3. Panel del profesional — YA EXISTE, solo mejoras incrementales

**El panel está construido y en uso. No rehacerlo, no migrarlo de librería, no rediseñarlo.**

Siendo agenda interna, este es el único frontend relevante hoy. Es una herramienta que se usa decenas de veces por día: el criterio de calidad acá es **denso, rápido y sin clics de más**, no vistoso. Un panel con mucho aire y animaciones se odia en una semana.

Las mejoras de abajo son **candidatas, no obligaciones**. Evaluar cuáles ya existen y cuáles aportan valor real sin tocar la estructura actual. Cada una debe poder implementarse de forma aislada.

### 3.1 Mejoras de interacción
- **Drag & drop para reprogramar**, si aún no está. Si el calendario actual no lo soporta nativamente, evaluar el costo antes de asumirlo: no vale cambiar de librería solo por esto.
  - Al soltar, la validación es **server-side** (reglas de §2.1 y §2.2), no solo visual.
  - Rollback visible si el backend rechaza el movimiento.
- **Optimistic UI** en las acciones frecuentes (cambiar estado, mover cita): reflejar al instante y confirmar contra el backend después. Es de las mejoras con mejor relación impacto/esfuerzo.
- **Acciones rápidas desde el mismo bloque** (cambiar estado, marcar no-show, abrir ficha) sin navegar a otra pantalla.
- **Búsqueda rápida de cliente por teclado**, sin mouse. Recepción trabaja con las manos ocupadas.
- **Atajos de teclado** para las 3-4 acciones más repetidas.

### 3.2 Mejoras de densidad y lectura
- Que el día completo entre **sin scroll** en la vista principal.
- **Colores por estado** consistentes y distinguibles (confirmado / pendiente / no-show / cancelado), con leyenda.
- **Vista multi-profesional** en columnas para el día, si todavía no existe. Cuando entren los recursos (§2.3), la misma vista debería poder agruparse por recurso.
- Indicador visual de **huecos muertos** (espacios chicos inutilizables entre citas). Conecta con §5.3.

### 3.3 Mejoras de percepción de velocidad
- **Skeletons en vez de spinners** al cargar el calendario.
- Estados vacíos resueltos (día sin citas, profesional sin horario cargado).
- Prefetch del día siguiente/anterior al navegar.

### 3.4 Ficha de cliente
- Abrirla en **panel lateral** sobre el calendario, no en otra página. Evita perder el contexto del día.
- Es el lugar natural donde va a vivir el timeline de §4.1.

### Disciplina visual
- **Respetar el sistema de componentes y los tokens que el proyecto ya usa.** No introducir una librería de UI nueva ni una segunda escala de spacing o tipografía.
- Si al auditar se detecta inconsistencia acumulada (varios tamaños de fuente, tres grises distintos, spacing arbitrario), **reportarla y proponer una unificación acotada** — no ejecutarla junto con una feature.

---

## 4. Prioridad ALTA — Ficha de cliente

### 4.1 Campos personalizados + timeline de visitas

La feature más subestimada y la que más diferencia frente a agendas genéricas. Es la **misma abstracción** para los tres rubros del pipeline: el odontólogo necesita antecedentes, el salón la fórmula del tinte y el historial de color, pilates lesiones y limitaciones.

- Definición de campos/formularios **por tenant** (texto, número, select, fecha, checkbox, archivo).
- **Timeline de visitas** en la ficha: cada cita puede llevar notas y adjuntos.
- Son configuración del tenant, no modelos distintos por rubro.

⚠️ Ver §7 sobre datos de salud.

### 4.2 Seguimiento de asistencia
- Estados de cita bien modelados, incluyendo **no-show** como estado propio y distinguible de cancelación.
- Historial de asistencia visible en la ficha del cliente.

### 4.3 Recalls y reactivación
- Regla por servicio: "volver a contactar a los N meses" → genera lista de clientes a llamar.
- Lista de clientes sin visita en N meses.

Al ser agenda interna, esto se entrega como **lista de trabajo para la recepción**, no como automatización hacia el cliente.

---

## 5. Prioridad MEDIA

### 5.1 Recordatorios salientes por WhatsApp
En el mercado objetivo el email prácticamente no se lee.

Alcance en esta etapa: **solo envío saliente**, sin interacción de vuelta del cliente (la confirmación con un tap es fase futura, §6).

- Recordatorio T-24h / T-2h, configurable por tenant.
- **Cola asíncrona con reintentos**, nunca dentro del request HTTP.
- Abstraer el canal detrás de una interfaz (`NotificationChannel`) para sumar email/SMS después sin tocar lógica de negocio.
- Si el proyecto ya tiene cola de tareas, usar la existente. Si no, ese es prerequisito de este punto.

### 5.2 Multi-profesional y compensación
- Horario, servicios habilitados y comisión por profesional.
- **Compensación configurable**: % o monto fijo por servicio. Parece cosa de salón pero es genérica.
- Reporte de liquidación por período.

*(Esto es cálculo interno de comisiones, no procesamiento de pagos.)*

### 5.3 Analítica operativa
Sobre datos propios. Nada de "IA" decorativa.

- Tasa de no-show por servicio, por horario y por profesional.
- **Huecos muertos**: detectar espacios inutilizables (ej. 30 min entre citas) y sugerir horarios que compacten el día.
- Ocupación por profesional y por recurso.

### 5.4 Mecanismos para mantenerlo genérico
Tres mecanismos. Con esto alcanza.

1. **Capability flags por tenant:** `usa_recursos`, `clases_grupales`, `usa_campos_personalizados`, `usa_comisiones`... La lógica es una sola; cambia qué se activa.
2. **Capa de vocabulario:** el mismo objeto se llama "clase", "turno", "cita" o "sesión" según el tenant. **Son labels, no modelos distintos.**
3. **Plantillas de onboarding por rubro (seed data):** servicios típicos, duraciones, campos de formulario, políticas por defecto, vocabulario. Arrancar con tres: pilates/fitness, estética/belleza, consultorio.

Es puro dato, y es lo que hace que un sistema genérico se sienta "hecho para mí".

### 5.5 Webhooks salientes
Que el tenant pueda enchufar su propia automatización a eventos de cita creada / cancelada / no-show.

---

## 6. Fase futura — requiere interacción del cliente final

**No implementar ahora.** Se documentan para que las decisiones de arquitectura de hoy no las bloqueen mañana.

- **Página pública de reservas por tenant** (subdominio propio, branding del tenant, mobile-first, flujo de 3 pasos, sin registro obligatorio).
- **Confirmación con un tap** por WhatsApp, con liberación automática del cupo si no confirma.
- **Lista de espera con auto-relleno**: al liberarse un cupo, se ofrece automáticamente a los que esperan, en orden, con ventana de aceptación. (Alto retorno económico, pero requiere que el cliente responda.)
- **Reprogramación autoservicio con link firmado**, sin crear cuenta.
- **Señas y pagos**: política configurable, medios locales (Bancard, Tigo Money, Pix).
- **Ledger de créditos / paquetes de sesiones** (packs de clases, planes de tratamiento, prepago).
- **Multi-moneda** Gs / USD / BRL / ARS con tasa del día, y página pública bilingüe español/portugués.
- **Integración con Google Calendar.** Fuera de alcance por decisión explícita. Si alguna vez se retoma: solo **push unidireccional**, con la base de datos propia como fuente de verdad y el calendario como proyección. Nunca sincronización bidireccional.

**Implicancia para hoy:** al modelar estados de cita, notificaciones y disponibilidad, dejar la puerta abierta a un origen de reserva externo (`created_by` / `source`) y a estados intermedios tipo "pendiente de confirmación". Sin construirlos todavía.

---

## 7. Precauciones

### Datos de salud ⚠️
Guardar historia clínica implica un nivel de sensibilidad, consentimiento, retención y auditoría distinto al de una agenda.

- Si se hace, **módulo aparte y opcional**, cifrado, con acceso auditado.
- **Antes de venderlo o describirlo como "historia clínica", verificar el marco regulatorio local aplicable.** Ahí cambia la promesa legal del producto. Esa verificación es responsabilidad del dueño del producto, no una decisión a tomar en código.
- Mientras tanto, "campos personalizados + notas de visita" (§4.1) cubre la necesidad funcional sin asumir esa promesa.

### No implementar
- Facturación electrónica, obras sociales/seguros, recetas, códigos de procedimiento.
- Inventario y consumo de insumos (si alguna vez, módulo opcional).
- Cualquier integración con calendarios externos, en cualquier dirección.

---

## 8. Orden de trabajo sugerido

Sujeto al resultado de la auditoría de §9: si algo ya está resuelto, se salta.

| # | Bloque | Motivo |
|---|--------|--------|
| 0 | **Auditoría del estado actual (§9)** | Nada arranca antes de esto. |
| 1 | Concurrencia y constraints (§2.1) | Riesgo de corrupción de datos si falta. |
| 2 | Campos personalizados + timeline (§4.1) | Mayor diferenciación frente a agendas genéricas. |
| 3 | Recursos (§2.3) | Habilita los tres rubros del pipeline con un solo motor. |
| 4 | Estados de cita + no-show + recalls (§4.2, §4.3) | Base de la analítica y del valor comercial. |
| 5 | Series recurrentes (§2.4) | Uso cotidiano en pilates y en controles. |
| 6 | Mejoras del panel (§3) | Incrementales y aisladas. Se intercalan según lo que habiliten los puntos anteriores. |
| 7 | Recordatorios WhatsApp salientes (§5.1) | Ataca el dolor #1 sin requerir interacción del cliente. |
| 8 | Capability flags + vocabulario + plantillas (§5.4) | Consolida la estrategia genérica. |
| 9 | Analítica operativa (§5.3) | Requiere datos acumulados; conviene después. |

---

## 9. Auditoría previa — responder antes de escribir código

No asumir que nada de esto existe. Revisar el sistema y reportar, punto por punto: **ya implementado / parcial / no existe**, con referencia a dónde vive en el código.

1. ¿Cómo está resuelta hoy la concurrencia al agendar? ¿Hay locks o constraints de unicidad en DB?
2. ¿Qué existe de reglas de disponibilidad (buffers, excepciones, duración por servicio, feriados)?
3. ¿Existe modelo de recursos, o solo de profesionales?
4. ¿Hay soporte de citas recurrentes / series? ¿Cómo maneja excepciones?
5. ¿Qué estados tiene una cita hoy? ¿Existe no-show como estado distinguible?
6. ¿Existe algún mecanismo de campos personalizados o notas por cliente?
7. ¿Hay cola de tareas asíncronas (Celery/RQ) y broker configurado?
8. ¿Cómo se manejan hoy las zonas horarias?
9. Sobre el panel existente: ¿qué librería de calendario usa, soporta drag & drop nativo, y qué sistema de componentes y tokens visuales están definidos? De la lista de §3, ¿qué ya está y qué falta?
10. ¿Existe algún mecanismo de configuración por tenant que sirva como base para los capability flags?
11. ¿Hay cifrado a nivel de campo disponible, de cara al módulo de datos sensibles de §7?

Con esas respuestas, proponer un plan ajustado — qué se salta por ya estar resuelto, qué se completa y qué se construye nuevo — **antes** de empezar a implementar.
