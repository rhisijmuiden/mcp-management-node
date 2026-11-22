// Package proto defines the JSON messages exchanged with MCP Management Node over the WebSocket.
//
// Agent -> app: hello, metrics, updates, res (answer to a req), job (progress of a long task).
// App -> agent: req (method + params), welcome.
package proto

import "encoding/json"

// Version of the message protocol. The app refuses agents with a higher major protocol.
const Version = 1

type Envelope struct {
	Type string `json:"type"`
	// req / res
	ID     string          `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Params json.RawMessage `json:"params,omitempty"`
	OK     bool            `json:"ok,omitempty"`
	Result any             `json:"result,omitempty"`
	Error  string          `json:"error,omitempty"`
	// hello / metrics / updates / welcome
	Data any `json:"data,omitempty"`
	// job
	JobID  string `json:"job_id,omitempty"`
	Event  string `json:"event,omitempty"` // line | done
	Line   string `json:"line,omitempty"`
	Status string `json:"status,omitempty"` // success | failed
}

type Hello struct {
	Protocol     int            `json:"protocol"`
	AgentVersion string         `json:"agent_version"`
	OS           string         `json:"os"`
	Arch         string         `json:"arch"`
	Hostname     string         `json:"hostname"`
	Info         map[string]any `json:"info"`
	Capabilities []string       `json:"capabilities"`
	Governor     *GovernorInfo  `json:"governor,omitempty"`
	Install      string         `json:"install"` // deb | binary | windows
}

type GovernorInfo struct {
	Current   string   `json:"current"`
	Available []string `json:"available"`
	Desired   string   `json:"desired,omitempty"`
	Driver    string   `json:"driver,omitempty"`
}

type Package struct {
	Name    string `json:"name"`
	Current string `json:"current"`
	New     string `json:"new"`
	Arch    string `json:"arch,omitempty"`
	Suite   string `json:"suite,omitempty"`
}

type Updates struct {
	CheckedAt      string    `json:"checked_at"`
	Packages       []Package `json:"packages"`
	RebootRequired bool      `json:"reboot_required"`
	RunningKernel  string    `json:"running_kernel,omitempty"`
	Error          string    `json:"error,omitempty"`
}
