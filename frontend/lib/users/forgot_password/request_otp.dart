import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
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
// ForgotPasswordPage
// ---------------------------------------------------------------------------

class ForgotPasswordPage extends StatefulWidget {
  const ForgotPasswordPage({super.key});

  @override
  State<ForgotPasswordPage> createState() => _ForgotPasswordPageState();
}

class _ForgotPasswordPageState extends State<ForgotPasswordPage>
    with SingleTickerProviderStateMixin {
  final _formKey         = GlobalKey<FormState>();
  final _emailController = TextEditingController();

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
    _emailController.dispose();
    super.dispose();
  }

  // ── Hit API forgot-password ──────────────────────────────────────────────
  Future<void> _handleRequestOtp() async {
    if (!_formKey.currentState!.validate()) return;
    
    if (!mounted) return; // Safety check pertama
    setState(() => _isLoading = true);

    try {
      final response = await _dio.post(
        '/users/reset-password/request-otp',
        data: {'email': _emailController.text.trim()},
      );

      if (!mounted) return; // Safety check setelah async
      
      if (response.statusCode == 200) {
        final data = response.data;
        
        // Cek apakah email terdaftar
        if (data['data']?['email_exists'] == true) {
          if (!mounted) return; // Safety check sebelum navigasi
          final email = Uri.encodeComponent(_emailController.text.trim());
          context.push('/users/reset-password/otp/verify-otp?email=$email');
        } else {
          _showErrorSnackbar('Email tidak terdaftar. Periksa kembali email Anda.');
        }
      }
    } on DioException catch (e) {
      if (!mounted) return;
      String message = 'Terjadi kesalahan. Coba lagi.';
      
      // Lebih detail error handling
      if (e.type == DioExceptionType.connectionTimeout) {
        message = 'Koneksi timeout. Periksa jaringan kamu.';
      } else if (e.type == DioExceptionType.receiveTimeout) {
        message = 'Server tidak merespons. Coba lagi nanti.';
      } else if (e.type == DioExceptionType.badResponse) {
        final statusCode = e.response?.statusCode;
        final detail = e.response?.data?['detail'];
        
        if (statusCode == 429) {
          message = 'Terlalu banyak permintaan. Coba lagi nanti.';
        } else if (detail != null) {
          message = detail.toString();
        } else {
          message = 'Terjadi kesalahan server.';
        }
      } else if (e.type == DioExceptionType.connectionError) {
        message = 'Tidak dapat terhubung ke server.';
      }
      
      _showErrorSnackbar(message);
    } catch (e) {
      if (!mounted) return;
      _showErrorSnackbar('Terjadi kesalahan tidak terduga.');
    } finally {
      if (mounted) setState(() => _isLoading = false);
    }
  }

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

  @override
  Widget build(BuildContext context) {
    return Scaffold(
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
                  // ── Tombol Kembali ─────────────────────────────────────
                  GestureDetector(
                    onTap: () => context.pop(),
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

                  // ── Heading ──────────────────────────────────────────────
                  Text(
                    'Lupa Password?',
                    style: GoogleFonts.playfairDisplay(
                      fontSize: 28,
                      fontWeight: FontWeight.w700,
                      color: BatikColors.sogaGelap,
                      letterSpacing: -0.5,
                    ),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    'Masukkan email yang terdaftar. Kami akan mengirimkan\n'
                    'kode OTP untuk verifikasi identitasmu.',
                    style: GoogleFonts.nunitoSans(
                      fontSize: 13,
                      color: BatikColors.abuTeks,
                      height: 1.6,
                    ),
                  ),
                  const SizedBox(height: 36),

                  // ── Email field ──────────────────────────────────────────
                  _NeonField(
                    controller: _emailController,
                    label: 'Email',
                    hint: 'johndoe@email.com',
                    icon: Icons.email_outlined,
                    keyboardType: TextInputType.emailAddress,
                    textInputAction: TextInputAction.done,
                    onFieldSubmitted: (_) => _handleRequestOtp(),
                    validator: (v) {
                      if (v == null || v.trim().isEmpty) {
                        return 'Email tidak boleh kosong';
                      }
                      final emailRegex =
                          RegExp(r'^[\w\.\-]+@[\w\-]+\.[a-zA-Z]{2,}$');
                      if (!emailRegex.hasMatch(v.trim())) {
                        return 'Format email tidak valid';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: 32),

                  // ── Submit button ────────────────────────────────────────
                  _NeonButton(
                    label: 'Kirim Kode OTP',
                    isLoading: _isLoading,
                    onPressed: _handleRequestOtp,
                  ),
                  const SizedBox(height: 28),

                  // ── Divider ──────────────────────────────────────────────
                  Row(
                    children: [
                      const Expanded(
                        child: Divider(
                          color: BatikColors.garis,
                          thickness: 1,
                        ),
                      ),
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 12),
                        child: Text(
                          'atau',
                          style: GoogleFonts.nunitoSans(
                            fontSize: 12,
                            color: BatikColors.abuTeks,
                          ),
                        ),
                      ),
                      const Expanded(
                        child: Divider(
                          color: BatikColors.garis,
                          thickness: 1,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 20),

                  // ── Back to login ────────────────────────────────────────
                  Center(
                    child: GestureDetector(
                      onTap: () => context.go('/users/login'),
                      child: RichText(
                        text: TextSpan(
                          style: GoogleFonts.nunitoSans(fontSize: 13),
                          children: [
                            TextSpan(
                              text: 'Ingat passwordnya? ',
                              style: const TextStyle(color: BatikColors.abuTeks),
                            ),
                            const TextSpan(
                              text: 'Masuk sekarang',
                              style: TextStyle(
                                color: BatikColors.marun,
                                fontWeight: FontWeight.w700,
                              ),
                            ),
                          ],
                        ),
                      ),
                    ),
                  ),
                ],
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
    this.obscureText = false,
    this.suffixIcon,
  });

  final TextEditingController controller;
  final String label;
  final String hint;
  final IconData icon;
  final bool obscureText;
  final TextInputType? keyboardType;
  final Widget? suffixIcon;
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
              obscureText: widget.obscureText,
              keyboardType: widget.keyboardType,
              textInputAction: widget.textInputAction,
              onFieldSubmitted: widget.onFieldSubmitted,
              validator: widget.validator,
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
                suffixIcon: widget.suffixIcon,
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