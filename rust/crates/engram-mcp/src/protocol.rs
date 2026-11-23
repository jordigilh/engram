//! JSON-RPC result builders kept separate from transport so both can be tested
//! without a listener.

use serde_json::{json, Value};

/// Protocol version currently advertised by the Python gateway. The Rust
/// gateway keeps this value during migration so clients negotiate identically.
pub const PROTOCOL_VERSION: &str = "2024-11-05";
pub const SERVER_NAME: &str = "engram-gateway";
pub const SERVER_VERSION: &str = "0.0.1";

/// MCP request-cancelled error code (JSON-RPC application error range).
pub const REQUEST_CANCELLED: i64 = -32800;
pub const METHOD_NOT_FOUND: i64 = -32601;

pub fn initialize_result(id: Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": { "tools": {} },
            "serverInfo": { "name": SERVER_NAME, "version": SERVER_VERSION }
        }
    })
}

pub fn tools_list_result(id: Value, tools: &[Value]) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "result": { "tools": tools }
    })
}

pub fn request_cancelled_error(id: Value) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {
            "code": REQUEST_CANCELLED,
            "message": "Request cancelled"
        }
    })
}

pub fn method_not_found_error(id: Value, method: &str) -> Value {
    json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {
            "code": METHOD_NOT_FOUND,
            "message": format!("Method not found: {method}")
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn initialize_matches_the_python_gateway_contract() {
        let result = initialize_result(json!(1));
        assert_eq!(result["jsonrpc"], "2.0");
        assert_eq!(result["id"], 1);
        assert_eq!(result["result"]["protocolVersion"], "2024-11-05");
        assert_eq!(result["result"]["capabilities"]["tools"], json!({}));
        assert_eq!(result["result"]["serverInfo"]["name"], "engram-gateway");
        assert_eq!(result["result"]["serverInfo"]["version"], "0.0.1");
    }

    #[test]
    fn tools_list_preserves_order_and_definitions() {
        let tools = vec![json!({"name": "alpha"}), json!({"name": "beta"})];
        let result = tools_list_result(json!("req-1"), &tools);
        assert_eq!(result["result"]["tools"][0]["name"], "alpha");
        assert_eq!(result["result"]["tools"][1]["name"], "beta");
    }

    #[test]
    fn cancellation_uses_the_mcp_code() {
        let result = request_cancelled_error(json!(42));
        assert_eq!(result["error"]["code"], -32800);
    }
}
