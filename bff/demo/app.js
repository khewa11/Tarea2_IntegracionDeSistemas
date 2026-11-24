// Demo web mínima: sin frameworks ni build. Todas las llamadas pasan por el Gateway
// (mismo origen) con un JWT de demostración obtenido en /auth/token.
"use strict";

const $ = (id) => document.getElementById(id);
const CLAVE_TOKEN = "demo.jwt";

let token = leer(CLAVE_TOKEN);
let sagaActual = null;
let pagoActual = null;

// ---------------------------------------------------------------- utilidades
function leer(clave) {
  try { return sessionStorage.getItem(clave); } catch { return null; }
}
function guardar(clave, valor) {
  try { valor == null ? sessionStorage.removeItem(clave) : sessionStorage.setItem(clave, valor); } catch { /* sin almacenamiento */ }
}

function mostrarAviso(texto, tipo = "error") {
  const aviso = $("aviso");
  aviso.textContent = texto;
  aviso.className = "aviso" + (tipo === "info" ? " info" : "");
  aviso.hidden = !texto;
}

function mensajeError(status, cuerpo) {
  const codigo = cuerpo && (cuerpo.error || cuerpo.detail);
  switch (status) {
    case 401: return "Sesión no válida (401): el token expiró o es inválido. Se pidió uno nuevo; intenta otra vez.";
    case 403: return "Operación no permitida desde la web (403).";
    case 404: return "No se encontró la reserva solicitada (404).";
    case 409: return "La reserva fue rechazada (409): " + (cuerpo && cuerpo.motivo || codigo || "conflicto") + ".";
    case 422: return "Datos inválidos (422): revisa patente y monto.";
    case 429: return "Demasiadas solicitudes (429): se superó el límite de " +
      ((cuerpo && cuerpo.limitePorVentana) || "N") + " por minuto. Espera un momento y reintenta.";
    case 502: return "El Gateway no pudo contactar un servicio interno (502).";
    case 503: return "Servicio no disponible (503)" + (cuerpo && cuerpo.motivo ? ": " + cuerpo.motivo : "") +
      ". El sistema rechazó la operación para protegerse; reintenta en unos segundos.";
    case 504: return "El servicio tardó demasiado en responder (504).";
    default: return "Error inesperado (" + status + ")" + (codigo ? ": " + codigo : "") + ".";
  }
}

// ------------------------------------------------------------------ red
async function pedirToken() {
  const r = await fetch("/auth/token", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ clienteId: "web-demo" }),
  });
  if (!r.ok) throw new Error(mensajeError(r.status, await r.json().catch(() => null)));
  token = (await r.json()).access_token;
  guardar(CLAVE_TOKEN, token);
  return token;
}

// Llamada autenticada. Ante un 401 renueva el token una vez y reintenta.
async function api(ruta, opciones = {}, reintentar = true) {
  if (!token) await pedirToken();
  const r = await fetch(ruta, {
    ...opciones,
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token, ...(opciones.headers || {}) },
  });
  const cuerpo = await r.json().catch(() => null);
  if (r.status === 401 && reintentar) {
    guardar(CLAVE_TOKEN, null);
    token = null;
    await pedirToken();
    return api(ruta, opciones, false);
  }
  return { status: r.status, cuerpo };
}

// -------------------------------------------------------------- espacios
async function cargarEspacios() {
  const select = $("espacio");
  const { status, cuerpo } = await api("/bff/dashboard");
  select.replaceChildren();
  if (status !== 200 || !Array.isArray(cuerpo && cuerpo.espacios)) {
    for (let i = 1; i <= 10; i++) select.add(new Option("E-" + String(i).padStart(3, "0"), "E-" + String(i).padStart(3, "0")));
    if (status !== 200) mostrarAviso(mensajeError(status, cuerpo));
    return;
  }
  for (const e of cuerpo.espacios) {
    const libre = e.estado === "DISPONIBLE";
    const opcion = new Option(e.espacioId + (libre ? " · disponible" : " · ocupado"), e.espacioId);
    opcion.disabled = !libre;
    select.add(opcion);
  }
  const primero = [...select.options].find((o) => !o.disabled);
  if (primero) select.value = primero.value;
}

// --------------------------------------------------------------- reserva
function nuevoSagaId() {
  // 22 caracteres: cabe completo como buy_order de Webpay (máx. 26).
  const hex = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now())
    .replace(/[^0-9a-f]/gi, "");
  return "S-" + hex.slice(0, 20);
}

async function reservar(evento) {
  evento.preventDefault();
  mostrarAviso("");
  const boton = $("reservar");
  boton.disabled = true;
  boton.textContent = "Procesando…";
  try {
    const datos = {
      espacioId: $("espacio").value,
      patente: $("patente").value.trim().toUpperCase(),
      monto: Number($("monto").value),
      sagaId: nuevoSagaId(),
    };
    const { status, cuerpo } = await api("/reservas-estacionamiento", { method: "POST", body: JSON.stringify(datos) });

    if (status === 202 && cuerpo && cuerpo.pago && cuerpo.pago.url) {
      // PASARELA=webpay: redirigir a Transbank con un form POST de token_ws.
      mostrarAviso("Redirigiendo a Webpay…", "info");
      irAWebpay(cuerpo.pago.url, cuerpo.pago.token);
      return;
    }
    if (cuerpo && cuerpo.sagaId) {
      // PASARELA=simulada: el resultado llega en la misma respuesta (201, 409 o 503).
      if (status !== 201) mostrarAviso(mensajeError(status, cuerpo), status === 503 ? "error" : "info");
      await mostrarReserva(cuerpo.sagaId);
      return;
    }
    mostrarAviso(mensajeError(status, cuerpo));
  } catch (e) {
    mostrarAviso(e.message || "No se pudo contactar el Gateway.");
  } finally {
    boton.disabled = false;
    boton.textContent = "Reservar";
  }
}

function irAWebpay(url, tokenWs) {
  const form = document.createElement("form");
  form.method = "POST";
  form.action = url;
  const campo = document.createElement("input");
  campo.type = "hidden";
  campo.name = "token_ws";
  campo.value = tokenWs;
  form.appendChild(campo);
  document.body.appendChild(form);
  form.submit();
}

// -------------------------------------------------------------- resultado
const CLASE_ESTADO = {
  COMPLETADA: "ok", PAGO_PENDIENTE: "pend", PAGO_COBRADO: "pend", ESPACIO_RESERVADO: "pend",
  INICIADA: "pend", COMPENSANDO: "pend", COMPENSADA: "mal", FALLIDA: "mal", COMPENSACION_PENDIENTE: "mal",
};
const MOTIVOS = {
  PAGO_RECHAZADO: "Webpay rechazó el pago; se liberó el espacio.",
  PAGO_ANULADO: "Pago anulado por el usuario en Webpay; se liberó el espacio.",
  PAGO_EXPIRADO_WEBPAY: "El formulario de Webpay expiró; se liberó el espacio.",
  PAGO_TIMEOUT: "No se completó el pago a tiempo; se liberó el espacio.",
  ESPACIO_OCUPADO: "El espacio ya estaba ocupado.",
};

function llenar(dl, pares) {
  dl.replaceChildren();
  for (const [etiqueta, valor] of pares) {
    if (valor === undefined || valor === null || valor === "") continue;
    const dt = document.createElement("dt");
    dt.textContent = etiqueta;
    const dd = document.createElement("dd");
    dd.textContent = String(valor);
    dl.append(dt, dd);
  }
  if (!dl.children.length) {
    const dd = document.createElement("dd");
    dd.textContent = "—";
    dl.append(dd);
  }
}

async function mostrarReserva(sagaId) {
  sagaActual = sagaId;
  const url = new URL(location.href);
  url.searchParams.set("sagaId", sagaId);
  url.searchParams.delete("error");
  history.replaceState(null, "", url);

  const { status, cuerpo } = await api("/bff/mi-reserva/" + encodeURIComponent(sagaId));
  if (status !== 200) {
    mostrarAviso(mensajeError(status, cuerpo));
    return;
  }
  $("vista-formulario").hidden = true;
  $("vista-resultado").hidden = false;

  const estado = cuerpo.estadoGeneral;
  $("r-saga").textContent = sagaId;
  const badge = $("r-estado");
  badge.textContent = estado.replaceAll("_", " ");
  badge.className = "estado " + (CLASE_ESTADO[estado] || "");
  const motivo = $("r-motivo");
  motivo.hidden = !cuerpo.motivo;
  motivo.textContent = cuerpo.motivo ? (MOTIVOS[cuerpo.motivo] || "Motivo: " + cuerpo.motivo) : "";

  $("r-degradado").hidden = !cuerpo.degradacionActiva;
  pagoActual = cuerpo.pagoPendiente || null;
  $("r-pagar").hidden = !pagoActual;

  const esp = cuerpo.espacio || {}, pago = cuerpo.pago || {}, acc = cuerpo.acceso || {};
  llenar($("b-saga"), [["Estado", estado], ["Motivo", cuerpo.motivo]]);
  llenar($("b-espacio"), [["Espacio", esp.espacioId], ["Estado", esp.estado]]);
  llenar($("b-pago"), [["Estado", pago.estado], ["Monto", pago.monto && "$" + pago.monto + " " + (pago.moneda || "")],
    ["Pasarela", pago.pasarela || (pago.referenciaPasarela && pago.referenciaPasarela.startsWith("SIM-") ? "Simulada" : null)],
    ["Orden de compra", pago.buyOrder], ["Autorización", pago.authorizationCode], ["Código respuesta", pago.responseCode]]);
  llenar($("b-acceso"), [["Estado", acc.estado], ["Patente", acc.patente], ["Detalle", acc.motivo]]);
}

// ---------------------------------------------------------------- inicio
async function iniciar() {
  try {
    if (!token) await pedirToken();
    $("sesion").textContent = "Sesión de demo activa";
    $("sesion").className = "chip ok";
  } catch (e) {
    $("sesion").textContent = "Sin sesión";
    $("sesion").className = "chip mal";
    mostrarAviso(e.message);
    return;
  }

  const params = new URLSearchParams(location.search);
  if (params.get("error")) {
    mostrarAviso(params.get("error") === "PAGO_NO_ENCONTRADO"
      ? "Webpay devolvió un pago que no corresponde a ninguna reserva."
      : "No se pudo registrar el retorno de Webpay: servicio no disponible.");
  }
  const saga = params.get("sagaId");
  if (saga) {
    await mostrarReserva(saga).catch((e) => mostrarAviso(e.message));
  }
  await cargarEspacios().catch((e) => mostrarAviso(e.message));
}

$("formulario").addEventListener("submit", reservar);
$("refrescar").addEventListener("click", () => sagaActual && mostrarReserva(sagaActual).catch((e) => mostrarAviso(e.message)));
$("ir-a-pagar").addEventListener("click", () => pagoActual && irAWebpay(pagoActual.url, pagoActual.token));
$("nueva").addEventListener("click", () => {
  sagaActual = null;
  mostrarAviso("");
  history.replaceState(null, "", location.pathname);
  $("vista-resultado").hidden = true;
  $("vista-formulario").hidden = false;
  cargarEspacios().catch((e) => mostrarAviso(e.message));
});
$("form-consulta").addEventListener("submit", (e) => {
  e.preventDefault();
  const id = $("consulta-saga").value.trim();
  if (id) { mostrarAviso(""); mostrarReserva(id).catch((err) => mostrarAviso(err.message)); }
});

iniciar();
