package winps

import (
	"encoding/base64"
	"testing"
)

func TestQuote(t *testing.T) {
	if got := Quote("it's"); got != "'it''s'" {
		t.Fatal(got)
	}
}

func TestEncode(t *testing.T) {
	b, _ := base64.StdEncoding.DecodeString(Encode("ab"))
	if string(b) != "a\x00b\x00" {
		t.Fatalf("%q", b)
	}
}
