import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:frontend/chats/batik_theme.dart';
import 'package:go_router/go_router.dart';
import 'package:google_fonts/google_fonts.dart';

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

final _dio = Dio(
  BaseOptions(
    baseUrl: 'http://localhost:8000',
    connectTimeout: const Duration(seconds: 10),
    receiveTimeout: const Duration(seconds: 10),
    headers: {'Content-Type': 'application/json'},
  ),
);

// ---------------------------------------------------------------------------
// Warna & konstanta — dipetakan dari palet batik
// ---------------------------------------------------------------------------

const _bg        = BatikColors.kertas;
const _neon      = BatikColors.marun;    // aksen utama (pengganti neon hijau)
const _neonDim   = Color(0x268B251E);    // marun 15%
const _surface   = BatikColors.putih;
const _border    = BatikColors.marun;
const _textMuted = BatikColors.abuTeks;

// ---------------------------------------------------------------------------
// ChangeEmailPage
// ---------------------------------------------------------------------------

class ChangeEmailPage extends StatefulWidget {
  const ChangeEmailPage({super.key, required this.token});

  /// change_token dari hasil verify OTP sebelumnya
  final String token;

  @override
  State<ChangeEmailPage> createState() => _ChangeEmailPageState();
}

class _ChangeEmailPageState extends State<ChangeEmailPage>
    with SingleTickerProviderStateMixin {
  final _formKey            = GlobalKey<FormState>();
  final _newEmailController = TextEditingController();

  bool _isLoading = false;

  late final AnimationController _fadeController;
  late final Animation<double>   _fadeAnimation;

  @override
  void initState() {
    super.initState();
    _fadeController = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 900),
    );
    _fadeAnimation = CurvedAnimation(
      parent: _fadeController,
      curve: Curves.easeOut,
    );
    _fadeController.forward();
  }

  @override
  void dispose() {
    _fadeController.dispose();
    _newEmailController.dispose();
    super.dispose();
  }

  // ── Hit API change-email ──────────────────────────────────────────────────

  Future<void> _handleChangeEmail() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() => _isLoading = true);

    try {
      final response = await _dio.post(
        '/users/change-email',
        data: {
          'token'    : widget.token,
          'new_email': _newEmailController.text.trim(),
        },
      );

      if (response.statusCode == 200 && mounted) {
        _showSuccessSnackbar('Email berhasil diubah.');
        await Future.delayed(const Duration(milliseconds: 800));
        if (mounted) context.go('/chats');
      }
    } on DioException catch (e) {
      if (!mounted) return;
      String message = 'Terjadi kesalahan. Coba lagi.';
      if (e.type == DioExceptionType.connectionTimeout ||
          e.type == DioExceptionType.receiveTimeout) {
        message = 'Koneksi timeout. Periksa jaringan kamu.';
      } else if (e.response?.data['detail'] != null) {
        message = e.response!.data['detail'].toString();
      }
      _showErrorSnackbar(message);
    } catch (_) {
      if (mounted) _showErrorSnackbar('Terjadi kesalahan tidak terduga.');
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

  // ── Snackbars ─────────────────────────────────────────────────────────────

  void _showErrorSnackbar(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          message,
          style: GoogleFonts.nunitoSans(
            fontSize: 13,
            color: BatikColors.sogaGelap,
          ),
        ),
        backgroundColor: BatikColors.putih,
        behavior: SnackBarBehavior.floating,
        margin: const EdgeInsets.all(16),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
          side: const BorderSide(color: BatikColors.marun, width: 1),
        ),
      ),
    );
  }

  void _showSuccessSnackbar(String message) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          message,
          style: GoogleFonts.nunitoSans(
            fontSize: 13,
            color: Colors.white,
          ),
        ),
        backgroundColor: BatikColors.hijau,
        behavior: SnackBarBehavior.floating,
        margin: const EdgeInsets.all(16),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
        ),
      ),
    );
  }

  // ── Build ─────────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    return CallbackShortcuts(
      bindings: <ShortcutActivator, VoidCallback>{
        // ⬇️ Tekan Enter untuk submit (selama loading tidak bisa)
        const SingleActivator(LogicalKeyboardKey.enter): () {
          if (!_isLoading) _handleChangeEmail();
        },
        // ⬇️ Tekan Numpad Enter juga bisa
        const SingleActivator(LogicalKeyboardKey.numpadEnter): () {
          if (!_isLoading) _handleChangeEmail();
        },
      },
      child: Focus(
        autofocus: true,
        child: Scaffold(
          backgroundColor: _bg,
          body: SafeArea(
            child: FadeTransition(
              opacity: _fadeAnimation,
              child: SingleChildScrollView(
                padding: const EdgeInsets.symmetric(horizontal: 28, vertical: 40),
                child: Form(
                  key: _formKey,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      // ── Tombol Kembali ────────────────────────────────────
                      GestureDetector(
                        onTap: () {
                          if (context.canPop()) {
                            context.pop();
                          }
                        },
                        child: Container(
                          width: 40,
                          height: 40,
                          decoration: BoxDecoration(
                            color: BatikColors.marun.withValues(alpha: 0.12),
                            borderRadius: BorderRadius.circular(12),
                            boxShadow: [
                              BoxShadow(
                                color: BatikColors.marun.withValues(alpha: 0.15),
                                blurRadius: 12,
                              ),
                            ],
                          ),
                          child: const Icon(
                            Icons.arrow_back_rounded,
                            color: BatikColors.marun,
                            size: 24,
                          ),
                        ),
                      ),
                      const SizedBox(height: 40),

                      // ── Heading ───────────────────────────────────────────
                      Text(
                        'Ganti Email',
                        style: GoogleFonts.playfairDisplay(
                          fontSize: 28,
                          fontWeight: FontWeight.w700,
                          color: BatikColors.sogaGelap,
                          letterSpacing: -0.5,
                        ),
                      ),
                      const SizedBox(height: 6),
                      Text(
                        'Masukkan alamat email baru untuk akunmu.',
                        style: GoogleFonts.nunitoSans(
                          fontSize: 13,
                          color: BatikColors.abuTeks,
                          height: 1.6,
                        ),
                      ),
                      const SizedBox(height: 36),

                      // ── Email baru ────────────────────────────────────────
                      _NeonField(
                        controller: _newEmailController,
                        label: 'Email Baru',
                        hint: 'contoh@email.com',
                        icon: Icons.mail_outline_rounded,
                        keyboardType: TextInputType.emailAddress,
                        textInputAction: TextInputAction.done,
                        onFieldSubmitted: (_) => _handleChangeEmail(),
                        validator: (v) {
                          if (v == null || v.trim().isEmpty) {
                            return 'Email tidak boleh kosong';
                          }
                          final emailRegex = RegExp(r'^[^@\s]+@[^@\s]+\.[^@\s]+$');
                          if (!emailRegex.hasMatch(v.trim())) {
                            return 'Format email tidak valid';
                          }
                          return null;
                        },
                      ),
                      const SizedBox(height: 32),

                      // ── Submit button ─────────────────────────────────────
                      _NeonButton(
                        label: 'Simpan Email Baru',
                        isLoading: _isLoading,
                        onPressed: _handleChangeEmail,
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Batik TextField (sebelumnya _NeonField)
// ---------------------------------------------------------------------------

class _NeonField extends StatefulWidget {
  const _NeonField({
    required this.controller,
    required this.label,
    required this.hint,
    required this.icon,
    this.keyboardType,
    this.validator,
    this.textInputAction,
    this.onFieldSubmitted,
  });

  final TextEditingController controller;
  final String label;
  final String hint;
  final IconData icon;
  final TextInputType? keyboardType;
  final String? Function(String?)? validator;
  final TextInputAction? textInputAction;
  final void Function(String)? onFieldSubmitted;

  @override
  State<_NeonField> createState() => _NeonFieldState();
}

class _NeonFieldState extends State<_NeonField> {
  bool _focused = false;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          widget.label,
          style: GoogleFonts.nunitoSans(
            fontSize: 12,
            fontWeight: FontWeight.w600,
            color: _focused ? BatikColors.marun : BatikColors.abuTeks,
            letterSpacing: 0.3,
          ),
        ),
        const SizedBox(height: 8),
        AnimatedContainer(
          duration: const Duration(milliseconds: 200),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(12),
            boxShadow: _focused
                ? [
                    BoxShadow(
                      color: BatikColors.marun.withValues(alpha: 0.15),
                      blurRadius: 16,
                    ),
                  ]
                : [],
          ),
          child: Focus(
            onFocusChange: (f) {
              if (mounted) setState(() => _focused = f);
            },
            child: TextFormField(
              controller: widget.controller,
              keyboardType: widget.keyboardType,
              validator: widget.validator,
              textInputAction: widget.textInputAction,
              onFieldSubmitted: widget.onFieldSubmitted,
              style: GoogleFonts.nunitoSans(
                fontSize: 14,
                color: BatikColors.sogaGelap,
                fontWeight: FontWeight.w500,
              ),
              cursorColor: BatikColors.marun,
              decoration: InputDecoration(
                hintText: widget.hint,
                hintStyle: GoogleFonts.nunitoSans(
                  fontSize: 14,
                  color: BatikColors.abuTeks,
                ),
                prefixIcon: Icon(
                  widget.icon,
                  color: _focused ? BatikColors.marun : BatikColors.abuTeks,
                  size: 20,
                ),
                filled: true,
                fillColor: _surface,
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 16,
                  vertical: 16,
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide:
                      const BorderSide(color: BatikColors.garis, width: 1.5),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide: const BorderSide(color: _border, width: 1.5),
                ),
                errorBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide:
                      const BorderSide(color: BatikColors.marun, width: 1.5),
                ),
                focusedErrorBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(12),
                  borderSide:
                      const BorderSide(color: BatikColors.marun, width: 1.5),
                ),
                errorStyle: GoogleFonts.nunitoSans(
                  fontSize: 11,
                  color: BatikColors.marun,
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
// Batik Button (sebelumnya _NeonButton)
// ---------------------------------------------------------------------------

class _NeonButton extends StatelessWidget {
  const _NeonButton({
    required this.label,
    required this.onPressed,
    this.isLoading = false,
  });

  final String label;
  final VoidCallback onPressed;
  final bool isLoading;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: double.infinity,
      height: 52,
      child: DecoratedBox(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(14),
          boxShadow: [
            BoxShadow(
              color: BatikColors.marun.withValues(alpha: 0.25),
              blurRadius: 20,
              offset: const Offset(0, 4),
            ),
          ],
        ),
        child: ElevatedButton(
          onPressed: isLoading ? null : onPressed,
          style: ElevatedButton.styleFrom(
            backgroundColor: BatikColors.marun,
            disabledBackgroundColor: BatikColors.marun.withValues(alpha: 0.5),
            foregroundColor: Colors.white,
            elevation: 0,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(14),
            ),
          ),
          child: isLoading
              ? const SizedBox(
                  width: 22,
                  height: 22,
                  child: CircularProgressIndicator(
                    strokeWidth: 2.5,
                    color: Colors.white,
                  ),
                )
              : Text(
                  label,
                  style: GoogleFonts.nunitoSans(
                    fontSize: 15,
                    fontWeight: FontWeight.w700,
                    color: Colors.white,
                    letterSpacing: 0.3,
                  ),
                ),
        ),
      ),
    );
  }
}