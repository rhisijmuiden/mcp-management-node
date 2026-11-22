package pkgmgr

import "testing"

func TestParseDnf(t *testing.T) {
	out := `
nginx.x86_64                 2:1.26.3-1.el10          appstream
openssl-libs.x86_64          1:3.2.2-16.el10_0.4      baseos
Obsoleting Packages
foo.noarch                   1.0-1                    baseos
`
	p := ParseDnf(out, map[string]string{"nginx": "1.26.2-1.el10"})
	if len(p) != 2 || p[0].Name != "nginx" || p[0].Current != "1.26.2-1.el10" || p[1].Arch != "x86_64" || p[1].Suite != "baseos" {
		t.Fatalf("%+v", p)
	}
}
