// lib/chats/bubble_chats.dart
import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_markdown/flutter_markdown.dart';
import 'package:frontend/chats/service_chats.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:frontend/chats/batik_theme.dart';
import 'package:markdown/markdown.dart' as md;
import 'package:url_launcher/url_launcher.dart';

// ---------------------------------------------------------------------------
// Platform Helpers
// ---------------------------------------------------------------------------

bool _isMobileDevice(BuildContext context) {
  final width = MediaQuery.of(context).size.width;
  return width < 768;
}

// ---------------------------------------------------------------------------
// Shared Bubbles
// ---------------------------------------------------------------------------

class AiAvatar extends StatelessWidget {
  const AiAvatar({super.key});

  @override
  Widget build(BuildContext context) => Container(
    width: 30,
    height: 30,
    decoration: const BoxDecoration(
      shape: BoxShape.circle,
      color: BatikColors.marun,
    ),
    child: const Icon(
      Icons.support_agent_rounded,
      color: Colors.white,
      size: 17,
    ),
  );
}

class UserBubble extends StatelessWidget {
  const UserBubble({super.key, required this.text, this.imageBytes});
  final String text;
  final Uint8List? imageBytes;

  @override
  Widget build(BuildContext context) {
    return Align(
      alignment: Alignment.centerRight,
      child: Container(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.65,
        ),
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
        decoration: BoxDecoration(
          color: BatikColors.marun,
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(16),
            topRight: Radius.circular(16),
            bottomLeft: Radius.circular(16),
            bottomRight: Radius.circular(4),
          ),
          boxShadow: [
            BoxShadow(
              color: BatikColors.marun.withValues(alpha: 0.18),
              blurRadius: 8,
              offset: const Offset(0, 3),
            ),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          mainAxisSize: MainAxisSize.min,
          children: [
            if (imageBytes != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 8),
                child: ClipRRect(
                  borderRadius: BorderRadius.circular(8),
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxHeight: 250),
                    child: Image.memory(
                      imageBytes!,
                      fit: BoxFit.contain,
                    ),
                  ),
                ),
              ),
            // Warna seleksi putih agar teks yang di-blok tetap terlihat di atas marun.
            DefaultSelectionStyle(
              selectionColor: Colors.white.withValues(alpha: 0.35),
              cursorColor: Colors.white,
              child: SelectionArea(
                child: Text(
                  text,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 14.5,
                    color: Colors.white,
                    height: 1.55,
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class AiBubble extends StatelessWidget {
  const AiBubble({super.key, required this.text, this.isError = false});
  final String text;
  final bool isError;

  @override
  Widget build(BuildContext context) {
    final defaultErrorText = 'Maaf, terjadi kesalahan saat memproses pertanyaan Anda.';
    final displayText = isError && text.isEmpty ? defaultErrorText : text;

    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (isError)
          Container(
            width: 30,
            height: 30,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: const Color(0xFFFFE4E1),
              border: Border.all(color: BatikColors.marun.withValues(alpha: 0.4)),
            ),
            child: const Icon(
              Icons.error_outline_rounded,
              color: BatikColors.marun,
              size: 16,
            ),
          )
        else
          const AiAvatar(),
        const SizedBox(width: 10),
        Expanded(
          child: Align(
            alignment: Alignment.centerLeft,
            // Batasi lebar baris agar nyaman dibaca di layar lebar.
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 760),
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                decoration: BoxDecoration(
                  color: isError ? const Color(0xFFFFF1EF) : BatikColors.putih,
                  borderRadius: const BorderRadius.only(
                    topLeft: Radius.circular(4),
                    topRight: Radius.circular(16),
                    bottomLeft: Radius.circular(16),
                    bottomRight: Radius.circular(16),
                  ),
                  border: Border.all(
                    color: isError
                        ? BatikColors.marun.withValues(alpha: 0.35)
                        : BatikColors.garis,
                  ),
                  boxShadow: [
                    BoxShadow(
                      color: BatikColors.sogaGelap.withValues(alpha: 0.05),
                      blurRadius: 6,
                      offset: const Offset(0, 2),
                    ),
                  ],
                ),
                child: DefaultSelectionStyle(
                  selectionColor: BatikColors.marun.withValues(alpha: 0.2),
                  cursorColor: BatikColors.marun,
                  child: SelectionArea(
                    child: isError
                        ? Text(
                            displayText,
                            style: GoogleFonts.nunitoSans(
                              fontSize: 14.5,
                              color: BatikColors.marunGelap,
                              height: 1.6,
                            ),
                          )
                        : _RichAiContent(data: displayText),
                  ),
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

// ---------------------------------------------------------------------------
// Rich AI Content — parse marker [[PRODUCT_CARD:...]]
// ---------------------------------------------------------------------------

/// Menerima teks dari LLM. Jika mengandung marker produk
/// `[[PRODUCT_CARD:id|motif|toko|harga|wa]]`, marker tersebut di-split
/// dan di-render sebagai widget kartu produk (bukan teks).
///
/// Jika tidak ada marker, seluruh teks di-render sebagai Markdown biasa.
class _RichAiContent extends StatelessWidget {
  const _RichAiContent({required this.data});
  final String data;

  static final _cardRegex = RegExp(r'\[\[PRODUCT_CARD:([^\]]+)\]\]');

  @override
  Widget build(BuildContext context) {
    final matches = _cardRegex.allMatches(data).toList();

    // Tidak ada marker → render markdown biasa
    if (matches.isEmpty) {
      return MarkdownBody(
        data: data,
        selectable: false,
        extensionSet: md.ExtensionSet.gitHubWeb,
        inlineSyntaxes: [_BreakSyntax()],
        softLineBreak: true,
        onTapLink: (text, href, title) {
          if (href != null) launchUrl(Uri.parse(href));
        },
        styleSheet: _markdownStyleSheet(context),
      );
    }

    // Ada marker → split & render bergantian
    final widgets = <Widget>[];
    int cursor = 0;

    for (final m in matches) {
      // Teks sebelum marker
      if (m.start > cursor) {
        final textPart = data.substring(cursor, m.start).trim();
        if (textPart.isNotEmpty) {
          widgets.add(MarkdownBody(
            data: textPart,
            selectable: false,
            extensionSet: md.ExtensionSet.gitHubWeb,
            inlineSyntaxes: [_BreakSyntax()],
            softLineBreak: true,
            onTapLink: (text, href, title) {
              if (href != null) launchUrl(Uri.parse(href));
            },
            styleSheet: _markdownStyleSheet(context),
          ));
        }
      }

      // Parse field dari marker
      final fields = m.group(1)!.split('|');
      if (fields.length >= 5) {
        widgets.add(Padding(
          padding: const EdgeInsets.symmetric(vertical: 6),
          child: _ProductCard(
            idUmkm:   fields[0].trim(),
            motif:    fields[1].trim(),
            toko:     fields[2].trim(),
            harga:    int.tryParse(fields[3].trim()) ?? 0,
            kontakWa: fields[4].trim(),
          ),
        ));
      } else {
        // Marker malformed → tampilkan sebagai teks apa adanya
        widgets.add(MarkdownBody(
          data: m.group(0)!,
          selectable: false,
          extensionSet: md.ExtensionSet.gitHubWeb,
          styleSheet: _markdownStyleSheet(context),
        ));
      }

      cursor = m.end;
    }

    // Sisa teks setelah marker terakhir
    if (cursor < data.length) {
      final tail = data.substring(cursor).trim();
      if (tail.isNotEmpty) {
        widgets.add(MarkdownBody(
          data: tail,
          selectable: false,
          extensionSet: md.ExtensionSet.gitHubWeb,
          inlineSyntaxes: [_BreakSyntax()],
          softLineBreak: true,
          onTapLink: (text, href, title) {
            if (href != null) launchUrl(Uri.parse(href));
          },
          styleSheet: _markdownStyleSheet(context),
        ));
      }
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: widgets,
    );
  }
}

// ---------------------------------------------------------------------------
// Product Card — kartu rekomendasi produk
// ---------------------------------------------------------------------------

class _ProductCard extends StatelessWidget {
  const _ProductCard({
    required this.idUmkm,
    required this.motif,
    required this.toko,
    required this.harga,
    required this.kontakWa,
  });

  final String idUmkm;
  final String motif;
  final String toko;
  final int    harga;
  final String kontakWa;

  String get _hargaFormatted {
    if (harga <= 0) return "-";
    final s = harga.toString();
    final buf = StringBuffer();
    for (int i = 0; i < s.length; i++) {
      if (i > 0 && (s.length - i) % 3 == 0) buf.write('.');
      buf.write(s[i]);
    }
    return "Rp$buf";
  }

  Future<void> _openWhatsApp() async {
    // Normalisasi nomor WA: hapus non-digit, pastikan format internasional
    var wa = kontakWa.replaceAll(RegExp(r'[^0-9]'), '');
    if (wa.startsWith('0')) {
      wa = '62${wa.substring(1)}';
    }
    if (wa.isEmpty) return;

    final url = Uri.parse('https://wa.me/$wa');
    try {
      if (await canLaunchUrl(url)) {
        await launchUrl(url, mode: LaunchMode.externalApplication);
      }
    } catch (_) {
      // silently ignore — user bisa coba lagi
    }
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: BatikColors.kertas,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: BatikColors.garis),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          // Motif
          Text(
            motif,
            style: GoogleFonts.playfairDisplay(
              color: BatikColors.sogaGelap,
              fontWeight: FontWeight.w700,
              fontSize: 15,
            ),
          ),
          const SizedBox(height: 4),

          // Toko
          Text(
            toko,
            style: GoogleFonts.nunitoSans(
              color: BatikColors.abuTeks,
              fontSize: 12.5,
            ),
          ),
          const SizedBox(height: 8),

          // Harga
          Row(
            children: [
              const Icon(
                Icons.local_offer_rounded,
                size: 14,
                color: BatikColors.emas,
              ),
              const SizedBox(width: 5),
              Text(
                "$_hargaFormatted / meter",
                style: GoogleFonts.nunitoSans(
                  color: BatikColors.marun,
                  fontSize: 13,
                  fontWeight: FontWeight.w800,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),

          // Tombol WhatsApp
          SizedBox(
            width: double.infinity,
            child: ElevatedButton.icon(
              onPressed: _openWhatsApp,
              icon: const Icon(Icons.chat_rounded, size: 16),
              label: Text(
                "Hubungi via WhatsApp",
                style: GoogleFonts.nunitoSans(
                  fontSize: 12.5,
                  fontWeight: FontWeight.w700,
                ),
              ),
              style: ElevatedButton.styleFrom(
                backgroundColor: BatikColors.hijau,
                foregroundColor: Colors.white,
                elevation: 0,
                padding: const EdgeInsets.symmetric(vertical: 10),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(8),
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Markdown syntax helper — <br> support
// ---------------------------------------------------------------------------

/// flutter_markdown tidak merender HTML, sehingga `<br>` (sering dipakai LLM
/// di dalam sel tabel, karena tabel Markdown tidak boleh berisi baris baru)
/// tampil sebagai teks literal. Syntax ini mengubahnya menjadi baris baru.
class _BreakSyntax extends md.InlineSyntax {
  _BreakSyntax() : super(r'<[bB][rR]\s*/?>');

  @override
  bool onMatch(md.InlineParser parser, Match match) {
    parser.addNode(md.Text('\n'));
    return true;
  }
}

/// Stylesheet markdown untuk balasan backend.
/// Semua teks memakai warna gelap (sogaGelap) karena bubble AI berlatar putih.
/// Dibangun dari tema agar elemen yang tidak didefinisikan (h4, del, dll.)
/// tidak jatuh ke warna bawaan yang tidak terduga.
MarkdownStyleSheet _markdownStyleSheet(BuildContext context) {
  final base = GoogleFonts.nunitoSans(
    fontSize: 14.5,
    color: BatikColors.sogaGelap,
    height: 1.65,
  );
  final heading = GoogleFonts.playfairDisplay(
    color: BatikColors.sogaGelap,
    fontWeight: FontWeight.w700,
    height: 1.35,
  );

  return MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
    p: base,
    strong: base.copyWith(fontWeight: FontWeight.w800),
    em: base.copyWith(fontStyle: FontStyle.italic),
    del: base.copyWith(
      decoration: TextDecoration.lineThrough,
      color: BatikColors.abuTeks,
    ),
    h1: heading.copyWith(fontSize: 21),
    h2: heading.copyWith(fontSize: 18),
    h3: heading.copyWith(fontSize: 16),
    h4: base.copyWith(fontWeight: FontWeight.w700),
    code: GoogleFonts.sourceCodePro(
      fontSize: 13,
      color: BatikColors.marunGelap,
      backgroundColor: BatikColors.krem,
    ),
    codeblockDecoration: BoxDecoration(
      color: BatikColors.krem,
      borderRadius: BorderRadius.circular(8),
      border: Border.all(color: BatikColors.garis),
    ),
    codeblockPadding: const EdgeInsets.all(14),
    listBullet: base.copyWith(
      color: BatikColors.marun,
      fontWeight: FontWeight.w700,
    ),
    listIndent: 20,
    blockquote: base.copyWith(
      color: BatikColors.soga,
      fontStyle: FontStyle.italic,
    ),
    blockquoteDecoration: BoxDecoration(
      color: BatikColors.krem,
      borderRadius: BorderRadius.circular(6),
      border: const Border(
        left: BorderSide(color: BatikColors.emas, width: 3),
      ),
    ),
    blockquotePadding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
    tableHead: base.copyWith(
      fontSize: 13.5,
      fontWeight: FontWeight.w700,
      color: BatikColors.marunGelap,
    ),
    tableBody: base.copyWith(fontSize: 13.5, height: 1.5),
    tableBorder: TableBorder.all(
      color: BatikColors.garis,
      borderRadius: BorderRadius.circular(6),
    ),
    tableCellsPadding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
    horizontalRuleDecoration: const BoxDecoration(
      border: Border(top: BorderSide(color: BatikColors.garis)),
    ),
    a: base.copyWith(
      color: BatikColors.marun,
      fontWeight: FontWeight.w600,
      decoration: TextDecoration.underline,
      decorationColor: BatikColors.marun.withValues(alpha: 0.5),
    ),
  );
}

// ---------------------------------------------------------------------------
// Action chips
// ---------------------------------------------------------------------------

class ActionChip extends StatelessWidget {
  const ActionChip({
    super.key,
    required this.icon,
    required this.label,
    required this.onTap,
    this.active = false,
  });

  final IconData icon;
  final String label;
  final VoidCallback onTap;
  final bool active;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(6),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(
          color: active
              ? BatikColors.marun.withValues(alpha: 0.15)
              : BatikColors.krem,
          borderRadius: BorderRadius.circular(6),
          border: Border.all(
            color: active
                ? BatikColors.marun.withValues(alpha: 0.5)
                : BatikColors.garis,
          ),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              icon,
              size: 13,
              color: active ? BatikColors.marun : BatikColors.abuTeks,
            ),
            const SizedBox(width: 5),
            Text(
              label,
              style: GoogleFonts.nunitoSans(
                fontSize: 11,
                color: active
                    ? BatikColors.marun
                    : BatikColors.abuTeks,
                fontWeight: FontWeight.w500,
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class AnswerActions extends StatelessWidget {
  const AnswerActions({
    super.key,
    required this.onRegenerate,
    required this.onCopy,
    required this.isPlayingTts,
    required this.onToggleTTS,
  });

  final VoidCallback onRegenerate;
  final VoidCallback onCopy;
  final bool isPlayingTts;
  final VoidCallback onToggleTTS;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: 40),
      child: Wrap(
        spacing: 8,
        runSpacing: 8,
        children: [
          ActionChip(
            icon: Icons.refresh_rounded,
            label: 'Generate ulang',
            onTap: onRegenerate,
          ),
          ActionChip(icon: Icons.copy_rounded, label: 'Salin', onTap: onCopy),
          ActionChip(
            icon: isPlayingTts
                ? Icons.volume_off_rounded
                : Icons.volume_up_rounded,
            label: isPlayingTts ? 'Stop Suara' : 'Dengarkan',
            onTap: onToggleTTS,
            active: isPlayingTts,
          ),
        ],
      ),
    );
  }
}

class PendingBubble extends StatefulWidget {
  const PendingBubble({super.key, required this.question, this.imageBytes});
  final String question;
  final Uint8List? imageBytes;

  @override
  State<PendingBubble> createState() => _PendingBubbleState();
}

class _PendingBubbleState extends State<PendingBubble>
    with SingleTickerProviderStateMixin {
  late final AnimationController _ctrl;
  late final Animation<double> _anim;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    )..repeat(reverse: true);
    _anim = CurvedAnimation(parent: _ctrl, curve: Curves.easeInOut);
  }

  @override
  void dispose() {
    _ctrl.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          UserBubble(text: widget.question, imageBytes: widget.imageBytes),
          const SizedBox(height: 12),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const AiAvatar(),
              const SizedBox(width: 10),
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 14,
                ),
                decoration: BoxDecoration(
                  color: BatikColors.putih,
                  borderRadius: const BorderRadius.only(
                    topLeft: Radius.circular(4),
                    topRight: Radius.circular(16),
                    bottomLeft: Radius.circular(16),
                    bottomRight: Radius.circular(16),
                  ),
                  border: Border.all(color: BatikColors.garis),
                ),
                child: FadeTransition(
                  opacity: _anim,
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: List.generate(
                      3,
                      (i) => Padding(
                        padding: EdgeInsets.only(left: i == 0 ? 0 : 5),
                        child: Container(
                          width: 7,
                          height: 7,
                          decoration: const BoxDecoration(
                            color: BatikColors.marun,
                            shape: BoxShape.circle,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class DisconnectedBubble extends StatelessWidget {
  const DisconnectedBubble({
    super.key,
    required this.message,
    required this.onResend,
  });
  final ChatMessage message;
  final VoidCallback onResend;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          UserBubble(text: message.question, imageBytes: message.localImageBytes),
          const SizedBox(height: 12),
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 30,
                height: 30,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  color: const Color(0x26C8962E),
                  border: Border.all(
                    color: BatikColors.emas.withValues(alpha: 0.4),
                  ),
                ),
                child: const Icon(
                  Icons.wifi_off_rounded,
                  color: BatikColors.emas,
                  size: 15,
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 14,
                  ),
                  decoration: BoxDecoration(
                    color: const Color(0xFFFFF8E7),
                    borderRadius: const BorderRadius.only(
                      topLeft: Radius.circular(4),
                      topRight: Radius.circular(16),
                      bottomLeft: Radius.circular(16),
                      bottomRight: Radius.circular(16),
                    ),
                    border: Border.all(
                      color: BatikColors.emas.withValues(alpha: 0.3),
                    ),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        'Koneksi terputus sebelum jawaban diterima.',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 13,
                          color: BatikColors.soga,
                          height: 1.5,
                        ),
                      ),
                      const SizedBox(height: 10),
                      GestureDetector(
                        onTap: onResend,
                        child: Container(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 14,
                            vertical: 8,
                          ),
                          decoration: BoxDecoration(
                            color: BatikColors.krem,
                            borderRadius: BorderRadius.circular(8),
                            border: Border.all(
                              color: BatikColors.emas.withValues(alpha: 0.5),
                            ),
                          ),
                          child: Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              const Icon(
                                Icons.refresh_rounded,
                                color: BatikColors.emas,
                                size: 14,
                              ),
                              const SizedBox(width: 6),
                              Text(
                                'Kirim ulang pertanyaan',
                                style: GoogleFonts.nunitoSans(
                                  fontSize: 12,
                                  color: BatikColors.sogaMuda,
                                  fontWeight: FontWeight.w500,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class StoppedBubble extends StatelessWidget {
  const StoppedBubble({
    super.key,
    required this.response,
    required this.onRegenerate,
    required this.onCopyAnswer,
    required this.isPlayingTts,
    required this.onToggleTTS,
  });

  final String response;
  final VoidCallback onRegenerate;
  final VoidCallback onCopyAnswer;
  final bool isPlayingTts;
  final VoidCallback onToggleTTS;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const AiAvatar(),
            const SizedBox(width: 10),
            Expanded(
              child: Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 12,
                ),
                decoration: BoxDecoration(
                  color: BatikColors.putih,
                  borderRadius: const BorderRadius.only(
                    topLeft: Radius.circular(4),
                    topRight: Radius.circular(16),
                    bottomLeft: Radius.circular(16),
                    bottomRight: Radius.circular(16),
                  ),
                  border: Border.all(color: BatikColors.garis),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (response.isNotEmpty) ...[
                      _RichAiContent(data: response),
                      const Divider(
                        color: BatikColors.garis,
                        height: 20,
                        thickness: 1,
                      ),
                    ],
                    Row(
                      children: [
                        const Icon(
                          Icons.stop_circle_outlined,
                          color: BatikColors.abuTeks,
                          size: 14,
                        ),
                        const SizedBox(width: 6),
                        Text(
                          'Generate dihentikan',
                          style: GoogleFonts.nunitoSans(
                            fontSize: 12,
                            color: BatikColors.abuTeks,
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 8),
        AnswerActions(
          onRegenerate: onRegenerate,
          onCopy: onCopyAnswer,
          isPlayingTts: isPlayingTts,
          onToggleTTS: onToggleTTS,
        ),
      ],
    );
  }
}

class MessagePair extends StatefulWidget {
  const MessagePair({
    super.key,
    required this.message,
    required this.onEdit,
    required this.onRegenerate,
    required this.onCopyQuestion,
    required this.onCopyAnswer,
    required this.isPlayingTts,
    required this.onToggleTTS,
  });

  final ChatMessage message;
  final void Function(String) onEdit;
  final VoidCallback onRegenerate;
  final VoidCallback onCopyQuestion;
  final VoidCallback onCopyAnswer;
  final bool isPlayingTts;
  final VoidCallback onToggleTTS;

  @override
  State<MessagePair> createState() => _MessagePairState();
}

class _MessagePairState extends State<MessagePair> {
  bool _hovered = false;

  void _showQuestionActions(BuildContext context) {
    final isMobile = _isMobileDevice(context);

    if (isMobile) {
      showModalBottomSheet(
        context: context,
        backgroundColor: BatikColors.putih,
        shape: const RoundedRectangleBorder(
          borderRadius: BorderRadius.vertical(top: Radius.circular(16)),
        ),
        builder: (ctx) => SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const SizedBox(height: 8),
              Container(
                width: 40,
                height: 4,
                decoration: BoxDecoration(
                  color: BatikColors.garis,
                  borderRadius: BorderRadius.circular(2),
                ),
              ),
              const SizedBox(height: 16),
              ListTile(
                leading: const Icon(
                  Icons.edit_outlined,
                  color: BatikColors.marun,
                ),
                title: Text(
                  'Edit pertanyaan',
                  style: GoogleFonts.nunitoSans(color: BatikColors.sogaGelap),
                ),
                onTap: () {
                  Navigator.pop(ctx);
                  _showEditDialog(context);
                },
              ),
              ListTile(
                leading: const Icon(
                  Icons.copy_rounded,
                  color: BatikColors.marun,
                ),
                title: Text(
                  'Salin pertanyaan',
                  style: GoogleFonts.nunitoSans(color: BatikColors.sogaGelap),
                ),
                onTap: () {
                  Navigator.pop(ctx);
                  widget.onCopyQuestion();
                },
              ),
              const SizedBox(height: 16),
            ],
          ),
        ),
      );
    }
  }

  void _showEditDialog(BuildContext context) {
    final ctrl = TextEditingController(text: widget.message.question);
    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: BatikColors.putih,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
        title: Text(
          'Edit Pertanyaan',
          style: GoogleFonts.nunitoSans(
            fontSize: 15,
            fontWeight: FontWeight.w700,
            color: BatikColors.sogaGelap,
          ),
        ),
        content: SizedBox(
          width: 480,
          child: TextField(
            controller: ctrl,
            autofocus: true,
            maxLines: null,
            style: GoogleFonts.nunitoSans(fontSize: 14, color: BatikColors.sogaGelap),
            cursorColor: BatikColors.marun,
            decoration: InputDecoration(
              filled: true,
              fillColor: BatikColors.kertas,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(10),
                borderSide: const BorderSide(color: BatikColors.garis),
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(10),
                borderSide: const BorderSide(color: BatikColors.garis),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(10),
                borderSide: const BorderSide(
                  color: BatikColors.marun,
                  width: 1.5,
                ),
              ),
            ),
          ),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx),
            child: Text(
              'Batal',
              style: GoogleFonts.nunitoSans(color: BatikColors.abuTeks),
            ),
          ),
          ElevatedButton(
            style: ElevatedButton.styleFrom(
              backgroundColor: BatikColors.marun,
              foregroundColor: Colors.white,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(8),
              ),
              elevation: 0,
            ),
            onPressed: () {
              final text = ctrl.text.trim();
              if (text.isNotEmpty && text != widget.message.question) {
                Navigator.pop(ctx);
                widget.onEdit(text);
              }
            },
            child: Text(
              'Simpan',
              style: GoogleFonts.nunitoSans(
                color: Colors.white,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final msg = widget.message;
    final isMobile = _isMobileDevice(context);
    final isError = msg.isFailed;

    return Padding(
      padding: const EdgeInsets.only(bottom: 24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (isMobile)
            GestureDetector(
              onLongPress: () => _showQuestionActions(context),
              child: UserBubble(text: msg.question, imageBytes: msg.localImageBytes),
            )
          else
            MouseRegion(
              onEnter: (_) => setState(() => _hovered = true),
              onExit: (_) => setState(() => _hovered = false),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.end,
                children: [
                  UserBubble(text: msg.question, imageBytes: msg.localImageBytes),
                  AnimatedOpacity(
                    opacity: _hovered && !msg.isStopped ? 1.0 : 0.0,
                    duration: const Duration(milliseconds: 150),
                    child: Padding(
                      padding: const EdgeInsets.only(top: 6, right: 4),
                      child: Row(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          ActionChip(
                            icon: Icons.edit_outlined,
                            label: 'Edit',
                            onTap: () => _showEditDialog(context),
                          ),
                          const SizedBox(width: 6),
                          ActionChip(
                            icon: Icons.copy_rounded,
                            label: 'Salin',
                            onTap: widget.onCopyQuestion,
                          ),
                        ],
                      ),
                    ),
                  ),
                  if (msg.isStopped)
                    AnimatedOpacity(
                      opacity: _hovered ? 1.0 : 0.0,
                      duration: const Duration(milliseconds: 150),
                      child: Padding(
                        padding: const EdgeInsets.only(top: 6, right: 4),
                        child: ActionChip(
                          icon: Icons.copy_rounded,
                          label: 'Salin',
                          onTap: widget.onCopyQuestion,
                        ),
                      ),
                    ),
                ],
              ),
            ),

          const SizedBox(height: 12),

          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              AiBubble(
                text: msg.response,
                isError: isError,
              ),
              const SizedBox(height: 8),
              AnswerActions(
                onRegenerate: widget.onRegenerate,
                onCopy: widget.onCopyAnswer,
                isPlayingTts: widget.isPlayingTts,
                onToggleTTS: widget.onToggleTTS,
              ),
            ],
          ),
        ],
      ),
    );
  }
}