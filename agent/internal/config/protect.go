package config

import "path/filepath"

// protectCommands are the icacls calls that limit dir to SYSTEM (S-1-5-18) and the
// Administrators group (S-1-5-32-544).
//
// The rights are set on the folder only, with inheritance flags (OI)(CI), and the files and
// folders below are then reset so they inherit them. 0.15.0 applied the grant with /T: icacls
// removed the inherited rights of every file but refused the (OI)(CI) grant on files, which left
// config.json, the keys and agent.log with an empty ACL that even SYSTEM could not read. The
// reset also repairs that.
func protectCommands(dir string) [][]string {
	return [][]string{
		{dir, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/C", "/Q"},
		{filepath.Join(dir, "*"), "/reset", "/T", "/C", "/Q"},
	}
}
