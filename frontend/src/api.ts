export type ApiResult = {
  allowed?: boolean;
  reason_code?: string | null;
  message?: string;
  detail?: string;
  request?: {
    request_id: string;
    origin_floor: number;
    call_type: "ORDINARY" | "FREIGHT";
    direction: "UP" | "DOWN" | null;
    status: string;
    assigned_car: string | null;
    wait_seconds: number | null;
    reassignment_count: number;
  };
  merged?: boolean;
  incident?: {
    incident_id: string;
    kind: string;
    elevator_id: string | null;
    floor: number | null;
    status: string;
    severity: string;
    details: string;
    history: string[];
    rescue_car_id: string | null;
  };
  session_token?: string;
  details?: {
    role?: string;
    passed?: boolean;
    total_records_inspected?: number;
    mismatch_index?: number | null;
    reason?: string;
  };
};

export async function authorizedApi<T>(
  path: string,
  token: string,
  data?: object,
  method?: "GET" | "POST" | "PATCH",
): Promise<T> {
  const response = await fetch(path, {
    method: method ?? (data === undefined ? "GET" : "POST"),
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  const result = await response.json() as T & { detail?: string; message?: string; reason_code?: string | null };
  if (!response.ok) {
    const baseMessage = result.message ?? result.detail ?? `HTTP ${response.status}`;
    throw new Error(result.reason_code ? `[${result.reason_code}] ${baseMessage}` : baseMessage);
  }
  return result;
}

export async function postApi(path: string, data: object): Promise<ApiResult> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const result = await response.json() as ApiResult;
  if (!response.ok) {
    const baseMessage = result.message ?? result.detail ?? `HTTP ${response.status}`;
    throw new Error(result.reason_code ? `[${result.reason_code}] ${baseMessage}` : baseMessage);
  }
  return result;
}

/** Present a credential at a car's reader; denials propagate without fake success. */
export async function swipeCabCard(
  elevatorId: string,
  credentialId: string,
  originFloor: number,
): Promise<ApiResult> {
  return postApi("/api/v1/acs/auth/cab/swipe", {
    elevator_id: elevatorId,
    credential_id: credentialId,
    origin_floor: originFloor,
  });
}

/** Request an in-cabin destination exclusively via ACS; never falls back to unauthenticated stubs. */
export async function requestCabDestination(
  elevatorId: string,
  originFloor: number,
  requestedFloor: number,
): Promise<ApiResult> {
  return postApi("/api/v1/acs/cab/destination", {
    elevator_id: elevatorId,
    origin_floor: originFloor,
    requested_floor: requestedFloor,
  });
}

